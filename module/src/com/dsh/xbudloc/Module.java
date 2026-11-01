package com.dsh.xbudloc;

import android.app.Activity;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.location.Criteria;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.MotionEvent;
import android.view.View;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * LSPosed module scoped to run.xbud.android.
 *
 * It replaces the framework location stream with a synthetic route so the app
 * sees a normal GPS fix: fresh elapsedRealtimeNanos, speed, bearing, altitude,
 * satellite count, and isMock() == false.  FakeLocation is not involved.
 *
 * Optional route file: /data/local/tmp/xbud_route.json
 *   {"speed":3.0,"points":[[lat,lng],[lat,lng],...]}
 */
public class Module implements IXposedHookLoadPackage {

    private static final String TAG = "XbudLoc";
    private static final String TARGET = "run.xbud.android";
    private static final String ROUTE_FILE = "/data/data/run.xbud.android/files/xbud_route.json";
    private static final long PERIOD_MS = 500L;

    private static final Object LOCK = new Object();
    private static final List<LocationListener> LISTENERS = new ArrayList<>();

    private static final Object ACCEL_LOCK = new Object();
    private static final List<SensorEventListener> ACCEL_LISTENERS = new ArrayList<>();
    private static final double CADENCE_HZ = 3.34; // 200 steps per minute
    private static Sensor accelerometer;
    private static Handler accelHandler;
    private static boolean accelRunning;
    private static long accelStartNanos;
    private static Constructor<SensorEvent> sensorEventCtor;

    private static final List<SensorEventListener> STEP_LISTENERS = new ArrayList<>();
    private static Sensor fakeStepCounter;
    private static Sensor fakeStepDetector;
    private static Handler stepHandler;
    private static boolean stepRunning;
    private static long stepCount;

    private static final String CMD_FILE = "/data/data/run.xbud.android/files/xbud_cmd";
    private static volatile Object pointsRef;
    private static ClassLoader appLoader;
    private static volatile Activity currentActivity;
    private static volatile String lastCommand = "";
    private static boolean cmdWatcherStarted;
    private static boolean mapHooked;
    private static volatile Object currentMap;
    private static volatile Object currentProjection;
    private static double curLat;
    private static double curLng;
    private static volatile boolean targetActive;
    private static volatile double targetLat;
    private static volatile double targetLng;
    private static volatile double targetSpeed = 3.0;
    private static boolean arrivedLogged;

    private static volatile boolean orbitActive;
    private static volatile boolean frozen;
    private static volatile boolean distStack;
    private static volatile boolean latLngLog = true;
    private static double orbitCenterLat;
    private static double orbitCenterLng;
    private static double orbitAngle;
    private static double orbitRadius = 30.0;

    private static Route route;
    private static Handler handler;
    private static boolean running;
    private static double travelled;
    private static int ticks;

    private static final Runnable TICK = new Runnable() {
        @Override
        public void run() {
            synchronized (LOCK) {
                if (!running) {
                    return;
                }
                Location location;
                double step = route.speedAt(travelled) * PERIOD_MS / 1000.0;
                if (orbitActive) {
                    orbitAngle += step / Math.max(orbitRadius, 1.0);
                    double mLat = 1.0 / 111320.0;
                    double mLng = 1.0 / (111320.0 * Math.cos(Math.toRadians(orbitCenterLat)));
                    curLat = orbitCenterLat + orbitRadius * Math.sin(orbitAngle) * mLat;
                    curLng = orbitCenterLng + orbitRadius * Math.cos(orbitAngle) * mLng;
                    location = makeLocation(curLat, curLng,
                            (Math.toDegrees(orbitAngle) + 90.0) % 360.0);
                } else if (targetActive) {
                    double dist = distance(curLat, curLng, targetLat, targetLng);
                    double moveStep = targetSpeed * PERIOD_MS / 1000.0;
                    if (dist > 4.0) {
                        double f = Math.min(1.0, moveStep / Math.max(dist, 0.1));
                        curLat += (targetLat - curLat) * f;
                        curLng += (targetLng - curLng) * f;
                        location = makeLocation(curLat, curLng,
                                bearing(curLat, curLng, targetLat, targetLng));
                    } else {
                        if (!arrivedLogged) {
                            arrivedLogged = true;
                            log("ARRIVED " + targetLat + "," + targetLng);
                        }
                        location = makeLocation(targetLat, targetLng, 0.0);
                    }
                } else {
                    if (!frozen) {
                        travelled += step;
                    }
                    location = route.locationAt(travelled);
                    curLat = location.getLatitude();
                    curLng = location.getLongitude();
                }
                for (LocationListener listener : new ArrayList<>(LISTENERS)) {
                    try {
                        listener.onLocationChanged(new Location(location));
                    } catch (Throwable ignored) {
                        // A dead listener must not stop the ticker.
                    }
                }
                if ((++ticks % 20) == 0) {
                    log("DELIVER listeners=" + LISTENERS.size()
                            + " at " + location.getLatitude() + "," + location.getLongitude());
                }
                handler.postDelayed(this, PERIOD_MS);
            }
        }
    };

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) {
        if (!TARGET.equals(lpparam.packageName)) {
            return;
        }
        appLoader = lpparam.classLoader;
        route = Route.load();
        log("loading for " + lpparam.processName + ", points=" + route.size()
                + ", speed=" + route.speed);

        hook("getLastKnownLocation", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                Location value = route.locationAt(travelled);
                param.setResult(value);
                log("GLKL " + (param.args != null && param.args.length > 0 ? param.args[0] : "?")
                        + " -> " + value.getLatitude() + "," + value.getLongitude());
            }
        }, String.class);

        hook("isProviderEnabled", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                param.setResult(Boolean.TRUE);
            }
        }, String.class);

        hook("isLocationEnabled", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                param.setResult(Boolean.TRUE);
            }
        });

        hook("getProviders", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                param.setResult(new ArrayList<>(Arrays.asList(
                        LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)));
            }
        }, boolean.class);

        hook("getAllProviders", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                param.setResult(new ArrayList<>(Arrays.asList(
                        LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)));
            }
        });

        hook("getBestProvider", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) {
                param.setResult(LocationManager.GPS_PROVIDER);
            }
        }, Criteria.class, boolean.class);

        try {
            XposedBridge.hookAllMethods(LocationManager.class, "requestLocationUpdates",
                    new XC_MethodHook() {
                        @Override
                        protected void beforeHookedMethod(MethodHookParam param) {
                            LocationListener listener = findListener(param.args);
                            if (listener == null) {
                                return;
                            }
                            log("RLU " + (param.args.length > 0 ? param.args[0] : "?")
                                    + " listener=" + listener.getClass().getName());
                            register(listener);
                            for (int i = 0; i < param.args.length; i++) {
                                if (param.args[i] == listener) {
                                    param.args[i] = new NoopListener();
                                }
                            }
                        }
                    });
        } catch (Throwable t) {
            log("requestLocationUpdates hook failed: " + t);
        }

        try {
            XposedBridge.hookAllMethods(Location.class, "isFromMockProvider",
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            param.setResult(Boolean.FALSE);
                        }
                    });
        } catch (Throwable ignored) {
        }

        try {
            XposedBridge.hookAllMethods(Location.class, "isMock", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    param.setResult(Boolean.FALSE);
                }
            });
        } catch (Throwable ignored) {
        }

        hookSensors();
        hookHttp();
        hookSsl();
        hookDistance();
        hookFenceToast();
        trackActivity();
        startCommandWatcher();
    }

    /** Log the stack that raises the "left the area" toast. */
    private static void hookFenceToast() {
        try {
            XposedBridge.hookAllMethods(android.widget.Toast.class, "show", new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    try {
                        Object text = XposedHelpers.getObjectField(param.thisObject, "mText");
                        String value = String.valueOf(text);
                        if (value.contains("脱离") || value.contains("有效区域")) {
                            log("FENCE " + value);
                            log("FENCE stack " + android.util.Log.getStackTraceString(
                                    new Throwable("fence")));
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
            log("fence toast hook installed");
        } catch (Throwable t) {
            log("fence toast hook failed: " + t);
        }
    }

    /** Reveal the coordinate space the app uses for checkpoint checks. */
    private static void hookDistance() {
        try {
            XposedBridge.hookAllMethods(Location.class, "distanceTo", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        Location from = (Location) param.thisObject;
                        Location to = (Location) param.args[0];
                        double value = (Double) param.getResult();
                        if (value < 5000) {
                            log("DIST " + from.getLatitude() + "," + from.getLongitude()
                                    + " -> " + to.getLatitude() + "," + to.getLongitude()
                                    + " = " + value);
                            logAppStack("DSTACK ");
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
            XposedBridge.hookAllMethods(Location.class, "distanceBetween", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        if (param.args != null && param.args.length >= 4) {
                            log("DISTB " + param.args[0] + "," + param.args[1]
                                    + " -> " + param.args[2] + "," + param.args[3]
                                    + " = " + java.util.Arrays.toString((float[]) param.args[4]));
                            logAppStack("DSTACKB ");
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
            log("distance hook installed");
        } catch (Throwable t) {
            log("distance hook failed: " + t);
        }
    }

    private static final java.util.Set<String> STACKS = new java.util.HashSet<>();

    /** Watch every FixedPointTable boolean setter: the checkpoint check calls it. */
    private static void hookPassSetters() {
        ClassLoader loader = appLoader;
        if (loader == null) {
            log("passhook: no loader");
            return;
        }
        try {
            Class<?> table = Class.forName(
                    "run.xbud.android.bean.database.FixedPointTable", false, loader);
            for (final Method method : table.getDeclaredMethods()) {
                if (method.getParameterCount() != 1
                        || method.getParameterTypes()[0] != boolean.class) {
                    continue;
                }
                final String name = method.getName();
                try {
                    XposedBridge.hookAllMethods(table, name, new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            StringBuilder sb = new StringBuilder();
                            for (StackTraceElement e : new Throwable().getStackTrace()) {
                                String cls = e.getClassName();
                                if (cls.startsWith("run.xbud") || cls.startsWith("com.xbud")) {
                                    sb.append(cls).append(".").append(e.getMethodName())
                                            .append(":").append(e.getLineNumber()).append(" < ");
                                }
                            }
                            log("PASSSET-CALL " + name + "(" + param.args[0] + ") "
                                    + "obj=" + trunc(String.valueOf(param.thisObject), 160)
                                    + " <- " + sb);
                        }
                    });
                    log("passhook: " + name);
                } catch (Throwable t) {
                    log("passhook skip " + name + ": " + t);
                }
            }
        } catch (Throwable t) {
            log("passhook failed: " + t);
        }
    }

    private static volatile boolean uploadComplete = true;
    private static volatile boolean beanPatch = true;

    /** Flip the client verdict on the upload bean right before it is signed/sent. */
    private static void forceUploadComplete(Object target) {
        if (!beanPatch || !uploadComplete || target == null) {
            return;
        }
        Class<?> type = target.getClass();
        if (!type.getName().endsWith("RunUploadBean")) {
            return;
        }
        try {
            java.lang.reflect.Field field = type.getDeclaredField("complete");
            field.setAccessible(true);
            boolean old = field.getBoolean(target);
            field.setBoolean(target, true);
            log("UPLOAD-COMPLETE " + old + " -> true");
        } catch (Throwable ignored) {
        }
    }

    /** Reveal how the upload signature is built: dump md5 input and output. */
    private static void hookDigest() {
        try {
            XC_MethodHook hook = new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        byte[] out = (byte[]) param.getResult();
                        if (out == null || out.length != 16) {
                            return;
                        }
                        StringBuilder hex = new StringBuilder();
                        for (byte b : out) {
                            hex.append(String.format("%02x", b));
                        }
                        Object input = param.args != null && param.args.length > 0
                                ? param.args[0] : null;
                        StringBuilder frames = new StringBuilder();
                        int n = 0;
                        for (StackTraceElement e : new Throwable().getStackTrace()) {
                            String cls = e.getClassName();
                            if (cls.startsWith("run.xbud") || cls.startsWith("com.xbud")
                                    || cls.startsWith("io.flutter")) {
                                frames.append(cls).append(".").append(e.getMethodName())
                                        .append(":").append(e.getLineNumber()).append(" < ");
                                if (++n >= 4) {
                                    break;
                                }
                            }
                        }
                        log("MD5 " + hex + " in=" + (input == null ? "<update-stream>"
                                : trunc(String.valueOf(input), 400)) + " <- " + frames);
                    } catch (Throwable ignored) {
                    }
                }
            };
            XposedBridge.hookAllMethods(java.security.MessageDigest.class, "digest", hook);
            log("digest hook installed");
        } catch (Throwable t) {
            log("digest hook failed: " + t);
        }
    }

    /** Log the first frames of the current stack, once per shape. */
    private static void logAppStack(String prefix) {
        if (!distStack) {
            return;
        }
        try {
            StringBuilder sb = new StringBuilder();
            int n = 0;
            for (StackTraceElement e : new Throwable().getStackTrace()) {
                String cls = e.getClassName();
                if (cls.startsWith("com.dsh.xbudloc") || cls.startsWith("de.robv")
                        || cls.startsWith("java.lang") || cls.startsWith("android.os")) {
                    continue;
                }
                sb.append(cls).append(".").append(e.getMethodName())
                        .append(":").append(e.getLineNumber()).append(" < ");
                if (++n >= 6) {
                    break;
                }
            }
            if (sb.length() == 0) {
                return;
            }
            String text = sb.toString();
            if (STACKS.add(text)) {
                log(prefix + text);
            }
        } catch (Throwable ignored) {
        }
    }

    private static boolean amapDistanceHooked;

    private static void hookAmapDistance(ClassLoader classLoader) {
        if (amapDistanceHooked) {
            return;
        }
        try {
            Class<?> utils = Class.forName("com.amap.api.maps.AMapUtils", false, classLoader);
            XposedBridge.hookAllMethods(utils, "calculateLineDistance", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        Object a = param.args[0];
                        Object b = param.args[1];
                        double lat1 = XposedHelpers.getDoubleField(a, "latitude");
                        double lng1 = XposedHelpers.getDoubleField(a, "longitude");
                        double lat2 = XposedHelpers.getDoubleField(b, "latitude");
                        double lng2 = XposedHelpers.getDoubleField(b, "longitude");
                        log("AMAPDIST " + lat1 + "," + lng1 + " -> " + lat2 + "," + lng2
                                + " = " + param.getResult());
                    } catch (Throwable ignored) {
                    }
                }
            });
            amapDistanceHooked = true;
            log("amap distance hook installed");
        } catch (Throwable t) {
            log("amap distance hook skipped: " + t);
        }
    }

    /** Tee the decrypted TLS stream: HTTP bodies are readable here. */
    private static void hookSsl() {
        String[] classes = {
                "com.android.org.conscrypt.ConscryptEngineSocket",
                "com.android.org.conscrypt.OpenSSLSocketImpl",
                "com.android.org.conscrypt.SSLSocketImpl"
        };
        for (String name : classes) {
            try {
                Class<?> type = Class.forName(name, false, Module.class.getClassLoader());
                XposedBridge.hookAllMethods(type, "getInputStream", new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        Object result = param.getResult();
                        if (result instanceof InputStream
                                && !(result instanceof TeeStream)) {
                            param.setResult(new TeeStream((InputStream) result));
                        }
                    }
                });
                log("ssl hook installed: " + name);
            } catch (Throwable t) {
                log("ssl hook skipped " + name + ": " + t);
            }
        }
    }

    private static final class TeeStream extends java.io.FilterInputStream {
        private final ByteArrayOutputStream buffer = new ByteArrayOutputStream();

        TeeStream(InputStream in) {
            super(in);
        }

        @Override
        public int read() throws java.io.IOException {
            int value = super.read();
            if (value >= 0) {
                buffer.write(value);
                maybeLog();
            }
            return value;
        }

        @Override
        public int read(byte[] data, int offset, int length) throws java.io.IOException {
            int count = super.read(data, offset, length);
            if (count > 0) {
                buffer.write(data, offset, count);
                maybeLog();
            }
            return count;
        }

        private void maybeLog() {
            if (buffer.size() < 256) {
                return;
            }
            String text = new String(buffer.toByteArray(), StandardCharsets.UTF_8);
            buffer.reset();
            if (text.contains("point") || text.contains("lat") || text.contains("run")
                    || text.contains("\"data\"")) {
                log("TLS " + text.substring(0, Math.min(text.length(), 3000)));
            }
        }
    }

    private static boolean flutterHooked;

    /** Dart -> Java platform channel calls carry the run configuration. */
    private static void hookFlutter(ClassLoader classLoader) {
        if (flutterHooked) {
            return;
        }
        String[] codecs = {
                "io.flutter.plugin.common.StandardMethodCodec",
                "io.flutter.plugin.common.JSONMethodCodec"
        };
        for (String name : codecs) {
            try {
                Class<?> codec = Class.forName(name, false, classLoader);                XposedBridge.hookAllMethods(codec, "decodeMethodCall", new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        Object call = param.getResult();
                        if (call == null) {
                            return;
                        }
                        try {
                            Object method = XposedHelpers.getObjectField(call, "method");
                            Object arguments = XposedHelpers.getObjectField(call, "arguments");
                            String text = String.valueOf(arguments);
                            if (text.length() > 3000) {
                                text = text.substring(0, 3000);
                            }
                            log("FLUTTER " + method + " " + text);
                        } catch (Throwable ignored) {
                        }
                    }
                });
                log("flutter codec hooked: " + name);
                flutterHooked = true;
            } catch (Throwable t) {
                log("flutter codec skipped " + name + ": " + t);
            }
        }
    }

    /** Capture API bodies: the assigned checkpoints arrive as JSON. */
    private static void hookHttp() {
        String[] classes = {
                "com.android.okhttp.internal.huc.HttpsURLConnectionImpl",
                "com.android.okhttp.internal.huc.HttpURLConnectionImpl"
        };
        for (String name : classes) {
            try {
                Class<?> type = Class.forName(name, false, Module.class.getClassLoader());
                XposedBridge.hookAllMethods(type, "getInputStream", new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        Object result = param.getResult();
                        if (!(result instanceof InputStream)) {
                            return;
                        }
                        try {
                            Object connection = param.thisObject;
                            Object contentType = XposedHelpers.callMethod(connection, "getContentType");
                            String type = contentType == null ? "" : contentType.toString();
                            if (!type.contains("json") && !type.contains("text")
                                    && !type.isEmpty()) {
                                return; // leave images and binary streams untouched
                            }
                            byte[] data = readAll((InputStream) result);
                            param.setResult(new ByteArrayInputStream(data));
                            String text = new String(data, StandardCharsets.UTF_8);
                            if (text.contains("point") || text.contains("lat")
                                    || text.contains("Point") || text.contains("校")) {
                                log("HTTP " + text.substring(0, Math.min(text.length(), 6000)));
                            }
                        } catch (Throwable t) {
                            log("HTTP read failed: " + t);
                        }
                    }
                });
                log("HTTP hook installed: " + name);
            } catch (Throwable t) {
                log("HTTP hook skipped " + name + ": " + t);
            }
        }
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int n;
        while ((n = in.read(buffer)) > 0) {
            out.write(buffer, 0, n);
        }
        return out.toByteArray();
    }

    /** Hook AMap lazily: its classes live in the app's runtime classloader. */
    private static void hookMapFromActivity(Activity activity) {
        String name = activity.getClass().getName();
        if (!name.contains("RunAdvanced") && !name.contains("RunStart")) {
            return;
        }
        View root = activity.getWindow() == null ? null : activity.getWindow().getDecorView();
        View mapView = findMapView(root);
        if (mapView == null) {
            return;
        }
        try {
            Object map = XposedHelpers.callMethod(mapView, "getMap");
            currentMap = map;
            Object projection = XposedHelpers.callMethod(map, "getProjection");
            if (projection != null) {
                currentProjection = projection;
            }
            if (mapHooked) {
                return;
            }
            Class<?> amap = map.getClass();
            if (!amap.getName().equals("com.amap.api.maps.AMap")) {
                amap = Class.forName("com.amap.api.maps.AMap", false,
                        mapView.getClass().getClassLoader());
            }
            XC_MethodHook hook = new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    for (Object arg : param.args) {
                        if (arg instanceof List) {
                            for (Object item : (List<?>) arg) {
                                dumpMarker(item);
                            }
                        } else {
                            dumpMarker(arg);
                        }
                    }
                }
            };
            XposedBridge.hookAllMethods(amap, "addMarker", hook);
            XposedBridge.hookAllMethods(amap, "addMarkers", hook);
            hookPolygon(amap, mapView.getClass().getClassLoader());
            hookProjectionCalls(amap, mapView.getClass().getClassLoader());
            mapHooked = true;
            log("AMap hooks installed via MapView");
            dumpExistingMarkers(map);
            hookProjection(map, mapView.getClass().getClassLoader());
        } catch (Throwable t) {
            log("AMap retry failed: " + t);
        }
    }

    private static View findMapView(View view) {
        if (view == null) {
            return null;
        }
        if (view.getClass().getName().contains("MapView")) {
            return view;
        }
        if (view instanceof android.view.ViewGroup) {
            android.view.ViewGroup group = (android.view.ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) {
                View found = findMapView(group.getChildAt(i));
                if (found != null) {
                    return found;
                }
            }
        }
        return null;
    }

    /** Every getProjection() returns a proxy that logs the LatLng it is given. */
    private static void hookProjectionCalls(Class<?> amap, ClassLoader loader) {
        try {
            final Class<?> iface = Class.forName(
                    "com.amap.api.maps.Projection", false, loader);
            XposedBridge.hookAllMethods(amap, "getProjection", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    final Object original = param.getResult();
                    if (original == null || Proxy.isProxyClass(original.getClass())) {
                        return;
                    }
                    Object wrapped = Proxy.newProxyInstance(
                            iface.getClassLoader(),
                            new Class<?>[]{iface},
                            new InvocationHandler() {
                                @Override
                                public Object invoke(Object proxy, Method method, Object[] args)
                                        throws Throwable {
                                    if (args != null) {
                                        for (Object arg : args) {
                                            logLatLng("PROJARG", arg);
                                        }
                                    }
                                    try {
                                        return method.invoke(original, args);
                                    } catch (java.lang.reflect.InvocationTargetException e) {
                                        throw e.getCause();
                                    }
                                }
                            });
                    param.setResult(wrapped);
                }
            });
            log("projection call proxy installed");
        } catch (Throwable t) {
            log("projection proxy failed: " + t);
        }
    }

    private static boolean gsonHooked;

    /** Watch the checkpoint model: isPass must flip when a point is reached. */
    private static void hookGson(ClassLoader classLoader) {
        if (gsonHooked) {
            return;
        }
        try {
            Class<?> gson = Class.forName("com.google.gson.Gson", false, classLoader);
            XposedBridge.hookAllMethods(gson, "fromJson", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (param.args != null && param.args.length > 0
                            && param.args[0] instanceof String) {
                        String text = (String) param.args[0];
                        if (text.contains("isPass") || text.contains("pointName")) {
                            log("GSON fromJson " + text.substring(0, Math.min(text.length(), 2500)));
                            Object result = param.getResult();
                            if (result instanceof List && text.contains("pointName")) {
                                pointsRef = result;
                                log("POINTS captured n=" + ((List<?>) result).size());
                            }
                        }
                    }
                }
            });
            XposedBridge.hookAllMethods(gson, "toJson", new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    if (param.args != null && param.args.length > 0 && param.args[0] != null) {
                        zeroUnCompleteReason(param.args[0]);
                        forceUploadComplete(param.args[0]);
                    }
                }

                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    Object result = param.getResult();
                    if (result instanceof String) {
                        String text = (String) result;
                        if (text.contains("unCompleteReason")) {
                            log("RECORD " + text.substring(0, Math.min(text.length(), 3000)));
                        }
                        if (text.contains("isPass") || text.contains("pointName")) {
                            if (param.args != null && param.args.length > 0
                                    && param.args[0] != null) {
                                Object source = param.args[0];
                                log("GSON class " + source.getClass().getName());
                                if (source instanceof java.util.List
                                        && !((java.util.List<?>) source).isEmpty()) {
                                    log("GSON elem " + ((java.util.List<?>) source)
                                            .get(0).getClass().getName());
                                }
                            }
                            log("GSON toJson " + text.substring(0, Math.min(text.length(), 2500)));
                        }
                    }
                }
            });
            gsonHooked = true;
            log("gson hooked");
        } catch (Throwable t) {
            log("gson hook skipped: " + t);
        }
    }

    private static boolean okhttpHooked;

    /** Capture Retrofit/OkHttp requests: URL, headers and JSON body. */
    private static void hookOkHttp(ClassLoader classLoader) {
        if (okhttpHooked) {
            return;
        }
        try {
            Class<?> builder = Class.forName("okhttp3.Request$Builder", false, classLoader);
            final ClassLoader loader = classLoader;
            XposedBridge.hookAllMethods(builder, "build", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        Object request = param.getResult();
                        Object url = XposedHelpers.callMethod(request, "url");
                        Object method = XposedHelpers.callMethod(request, "method");
                        String text = String.valueOf(url);
                        boolean interesting = text.contains("xbud") || text.contains("run")
                                || text.contains("point") || text.contains("sport")
                                || text.contains("record") || text.contains("upload");
                        if (interesting) {
                            Object headers = XposedHelpers.callMethod(request, "headers");
                            log("REQ " + method + " " + text + " " + headers);
                            Object body = XposedHelpers.callMethod(request, "body");
                            if (body != null) {
                                Object contentType = XposedHelpers.callMethod(body, "contentType");
                                String type = String.valueOf(contentType);
                                if (type.contains("json") || type.contains("form")
                                        || type.contains("text")) {
                                    Class<?> bufferClass = Class.forName("okio.Buffer", false, loader);
                                    Object buffer = bufferClass.newInstance();
                                    XposedHelpers.callMethod(body, "writeTo", buffer);
                                    Object payload = XposedHelpers.callMethod(buffer, "readUtf8");
                                    String value = String.valueOf(payload);
                                    log("REQBODY " + method + " " + text + " -> "
                                            + value.substring(0, Math.min(value.length(), 4000)));
                                }
                            }
                        }
                    } catch (Throwable t) {
                        log("REQ dump failed: " + t);
                    }
                }
            });
            okhttpHooked = true;
            log("okhttp request hook installed");
        } catch (Throwable t) {
            log("okhttp request hook skipped: " + t);
        }

        try {
            Class<?> buffer = Class.forName("okio.Buffer", false, classLoader);
            XC_MethodHook bodyHook = new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (param.args == null || param.args.length == 0) {
                        return;
                    }
                    Object first = param.args[0];
                    if (!(first instanceof String)) {
                        return;
                    }
                    String text = (String) first;
                    if (text.contains("pid") || text.contains("isPass")
                            || text.contains("point") || text.contains("runId")) {
                        log("BODY " + text.substring(0, Math.min(text.length(), 2500)));
                    }
                }
            };
            XposedBridge.hookAllMethods(buffer, "writeUtf8", bodyHook);
            XposedBridge.hookAllMethods(buffer, "writeString", bodyHook);
            log("okio body hook installed");
        } catch (Throwable t) {
            log("okio body hook skipped: " + t);
        }
    }

    private static boolean sportJniHooked;

    /** Read the request/response payloads before/after native AES. */
    private static void hookSportJni(ClassLoader classLoader) {
        if (sportJniHooked) {
            return;
        }
        try {
            Class<?> cls = Class.forName("com.xbud.run.sport.SportJniUtils", false, classLoader);
            XposedBridge.hookAllMethods(cls, "encodeByAES", new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    log("ENC-IN " + truncate(param.args));
                }

                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    log("ENC-OUT " + truncate(new Object[]{param.getResult()}));
                }
            });
            XposedBridge.hookAllMethods(cls, "decodeByAES", new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    log("DEC-IN " + truncate(param.args));
                }

                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    log("DEC-OUT " + truncate(new Object[]{param.getResult()}));
                }
            });
            sportJniHooked = true;
            log("sport jni hooked");
        } catch (Throwable t) {
            log("sport jni hook skipped: " + t);
        }
    }

    private static String truncate(Object[] values) {
        if (values == null || values.length == 0) {
            return "null";
        }
        String text = String.valueOf(values[0]);
        return text.length() > 4000 ? text.substring(0, 4000) : text;
    }

    private static boolean pointModelHooked;

    /** Force every read of the checkpoint entity to report isPass = true. */
    private static void hookPointModel(ClassLoader classLoader) {
        if (pointModelHooked) {
            return;
        }
        try {
            Class<?> table = Class.forName(
                    "run.xbud.android.bean.database.FixedPointTable", false, classLoader);
            XC_MethodHook alwaysTrue = new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    param.setResult(Boolean.TRUE);
                }
            };
            String[] getters = {"isPass", "getPass", "getIsPass"};
            for (String name : getters) {
                try {
                    XposedBridge.hookAllMethods(table, name, alwaysTrue);
                    log("point getter hooked: " + name);
                } catch (Throwable ignored) {
                }
            }
            pointModelHooked = true;
        } catch (Throwable t) {
            log("point model hook skipped: " + t);
        }
    }

    private static void forcePass(Object item) {
        if (item == null) {
            return;
        }
        try {
            java.lang.reflect.Field field = item.getClass().getDeclaredField("isPass");
            field.setAccessible(true);
            field.setBoolean(item, true);
            log("FORCE isPass=true " + item.getClass().getName());
            return;
        } catch (Throwable ignored) {
        }
        try {
            XposedHelpers.callMethod(item, "setPass", true);
            log("FORCE setPass " + item.getClass().getName());
        } catch (Throwable ignored) {
        }
    }

    private static void dumpFields(Activity activity) {
        try {
            Class<?> type = activity.getClass();
            while (type != null && type != Object.class && type != Activity.class) {
                for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                    try {
                        log("FIELD " + type.getSimpleName() + "." + field.getName()
                                + " : " + field.getType().getSimpleName());
                    } catch (Throwable ignored) {
                    }
                }
                type = type.getSuperclass();
            }
        } catch (Throwable t) {
            log("field dump failed: " + t);
        }
    }

    /** Full dump: field values plus list element types and toString previews. */
    private static void dumpFieldsDeep(Activity activity) {
        Class<?> type = activity.getClass();
        int depth = 0;
        while (type != null && type != Object.class && type != Activity.class && depth < 5) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    Object value = field.get(activity);
                    log("F " + type.getSimpleName() + "." + field.getName() + " : "
                            + field.getType().getSimpleName() + " = " + preview(value));
                    if (value != null && !(value instanceof List)
                            && !field.getType().getName().startsWith("android.")
                            && !field.getType().getName().startsWith("java.")) {
                        dumpObjectFields(value, "  ." + field.getName(), 1);
                    }
                } catch (Throwable t) {
                    log("F " + type.getSimpleName() + "." + field.getName() + " err " + t);
                }
            }
            type = type.getSuperclass();
            depth++;
        }
    }

    private static void dumpObjectFields(Object target, String prefix, int depth) {
        if (target == null || depth > 2) {
            return;
        }
        Class<?> type = target.getClass();
        if (type.getName().startsWith("android.") || type.getName().startsWith("java.")) {
            return;
        }
        while (type != null && type != Object.class && depth <= 2) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    Object value = field.get(target);
                    log("F" + prefix + "." + field.getName() + " : "
                            + field.getType().getSimpleName() + " = " + preview(value));
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
            depth++;
        }
    }

    private static String preview(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof List) {
            List<?> list = (List<?>) value;
            StringBuilder sb = new StringBuilder("List(").append(list.size()).append(")");
            int n = 0;
            for (Object item : list) {
                if (n++ >= 3) {
                    break;
                }
                sb.append(" [").append(item == null ? "null" : item.getClass().getName())
                        .append(" ").append(item == null ? "" : trunc(String.valueOf(item), 260))
                        .append("]");
            }
            return sb.toString();
        }
        return trunc(String.valueOf(value), 300);
    }

    private static String trunc(String text, int max) {
        return text.length() > max ? text.substring(0, max) : text;
    }

    private static int markerCount;
    /** Walk the activity graph and log every AMap marker position. */
    private static void dumpMarkers(Activity activity) {
        markerCount = 0;
        Class<?> type = activity.getClass();
        while (type != null && type != Object.class && type != Activity.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    walkMarkers(field.get(activity), 0);
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
        log("MARKERS total=" + markerCount);
    }

    private static void walkMarkers(Object value, int depth) {
        if (value == null || depth > 3) {
            return;
        }
        String cls = value.getClass().getName();
        if (value instanceof List) {
            for (Object item : new ArrayList<Object>((List<?>) value)) {
                walkMarkers(item, depth + 1);
            }
            return;
        }
        if (cls.endsWith(".Marker")) {
            try {
                Object pos = XposedHelpers.callMethod(value, "getPosition");
                Object title = XposedHelpers.callMethod(value, "getTitle");
                Object snippet = XposedHelpers.callMethod(value, "getSnippet");
                double lat = XposedHelpers.getDoubleField(pos, "latitude");
                double lng = XposedHelpers.getDoubleField(pos, "longitude");
                markerCount++;
                log("MARKER " + title + " | " + snippet + " | " + lat + "," + lng);
            } catch (Throwable ignored) {
            }
            return;
        }
        if (cls.startsWith("android.") || cls.startsWith("java.")
                || cls.startsWith("com.amap") || cls.startsWith("androidx.")
                || cls.startsWith("org.") || cls.startsWith("kotlin.")) {
            return;
        }
        Class<?> type = value.getClass();
        int level = 0;
        while (type != null && type != Object.class && level < 2) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    walkMarkers(field.get(value), depth + 1);
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
            level++;
        }
    }

    /** Dump every view with an id or text, with on-screen bounds. */
    private static void dumpViews(Activity activity) {
        View root = activity.getWindow() == null ? null : activity.getWindow().getDecorView();
        walkView(root, 0);
        log("VIEWS done");
    }

    private static void walkView(View view, int depth) {
        if (view == null || depth > 14) {
            return;
        }
        try {
            String id = "";
            int vid = view.getId();
            if (vid != View.NO_ID) {
                try {
                    id = view.getResources().getResourceEntryName(vid);
                } catch (Throwable ignored) {
                    id = String.valueOf(vid);
                }
            }
            String text = "";
            if (view instanceof android.widget.TextView) {
                text = String.valueOf(((android.widget.TextView) view).getText());
            }
            if (!id.isEmpty() || !text.isEmpty()) {
                int[] xy = new int[2];
                view.getLocationOnScreen(xy);
                log("V " + depth + " " + view.getClass().getSimpleName() + " id=" + id
                        + " " + xy[0] + "," + xy[1] + " " + view.getWidth() + "x"
                        + view.getHeight() + " vis=" + view.getVisibility()
                        + (text.isEmpty() ? "" : " t=" + trunc(text, 24)));
            }
            if (view instanceof android.view.ViewGroup) {
                android.view.ViewGroup group = (android.view.ViewGroup) view;
                for (int i = 0; i < group.getChildCount(); i++) {
                    walkView(group.getChildAt(i), depth + 1);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    /** Read-only state dump of point-like objects (isPass & friends). */
    private static void dumpPointState(Activity activity) {
        int hits = 0;
        Class<?> type = activity.getClass();
        while (type != null && type != Object.class && type != Activity.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    Object value = field.get(activity);
                    if (value instanceof List) {
                        for (Object item : new ArrayList<Object>((List<?>) value)) {
                            hits += logPointFields(item);
                        }
                    } else {
                        hits += logPointFields(value);
                    }
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
        log("STATE objects=" + hits);
    }

    private static int logPointFields(Object item) {
        if (item == null) {
            return 0;
        }
        String cls = item.getClass().getName();
        String text = String.valueOf(item);
        if (!cls.toLowerCase().contains("point") && !text.contains("pointName")) {
            return 0;
        }
        log("STATE " + cls + " :: " + trunc(text, 160));
        Class<?> type = item.getClass();
        while (type != null && type != Object.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    if (field.getType().isPrimitive() || field.getType() == String.class) {
                        log("  S " + field.getName() + " = " + field.get(item));
                    }
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
        return 1;
    }

    private static void dumpPointList() {
        Object ref = pointsRef;
        if (!(ref instanceof List)) {
            log("points: none captured");
            return;
        }
        List<?> list = (List<?>) ref;
        log("points n=" + list.size());
        for (Object item : list) {
            logAllFields(item);
        }
    }

    private static void flipPointList() {
        Object ref = pointsRef;
        if (!(ref instanceof List)) {
            log("flip: none captured");
            return;
        }
        for (Object item : (List<?>) ref) {
            forcePassObject(item);
        }
    }

    private static void logAllFields(Object item) {
        if (item == null) {
            return;
        }
        log("POBJ " + item.getClass().getName());
        Class<?> type = item.getClass();
        while (type != null && type != Object.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    if (field.getType().isPrimitive() || field.getType() == String.class) {
                        log("  P " + field.getName() + " = " + field.get(item));
                    }
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
    }

    private static boolean textHooked;

    /** Log which app code fills the requirement rows / icons. */
    private static void hookText() {
        if (textHooked) {
            return;
        }
        textHooked = true;
        try {
            XposedBridge.hookAllMethods(android.widget.TextView.class, "setText",
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            try {
                                if (param.args == null || param.args.length == 0
                                        || !(param.args[0] instanceof CharSequence)) {
                                    return;
                                }
                                String text = param.args[0].toString();
                                if (!text.contains("点位") && !text.contains("配速")
                                        && !text.contains("公里") && !text.contains("要求")) {
                                    return;
                                }
                                StringBuilder sb = new StringBuilder();
                                for (StackTraceElement e : new Throwable().getStackTrace()) {
                                    String cls = e.getClassName();
                                    if (cls.startsWith("run.xbud") || cls.startsWith("com.xbud")) {
                                        sb.append(cls).append(".").append(e.getMethodName())
                                                .append(":").append(e.getLineNumber()).append(" < ");
                                    }
                                }
                                log("TXT " + trunc(text, 40) + " <- " + sb);
                            } catch (Throwable ignored) {
                            }
                        }
                    });
            XposedBridge.hookAllMethods(android.widget.ImageView.class, "setImageDrawable",
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            try {
                                StringBuilder sb = new StringBuilder();
                                for (StackTraceElement e : new Throwable().getStackTrace()) {
                                    String cls = e.getClassName();
                                    if (cls.startsWith("run.xbud") || cls.startsWith("com.xbud")) {
                                        sb.append(cls).append(".").append(e.getMethodName())
                                                .append(":").append(e.getLineNumber()).append(" < ");
                                    }
                                }
                                String cls = param.thisObject.getClass().getName();
                                if (sb.length() > 0) {
                                    log("IMGD " + cls + " <- " + sb);
                                }
                            } catch (Throwable ignored) {
                            }
                        }
                    });
            log("text hooks installed");
        } catch (Throwable t) {
            log("text hooks failed: " + t);
        }
    }

    private static boolean imgHooked;

    /** Reveal who sets the requirement ticks: log ImageView resources + stack. */
    private static void hookImages() {
        if (imgHooked) {
            return;
        }
        imgHooked = true;
        try {
            XposedBridge.hookAllMethods(android.widget.ImageView.class, "setImageResource",
                    new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            try {
                                int id = (Integer) param.args[0];
                                String name = "";
                                try {
                                    name = param.thisObject.getClass().getPackage() != null
                                            ? ((android.view.View) param.thisObject).getResources()
                                            .getResourceEntryName(id) : "";
                                } catch (Throwable ignored) {
                                }
                                StringBuilder sb = new StringBuilder();
                                StackTraceElement[] stack = new Throwable().getStackTrace();
                                for (StackTraceElement element : stack) {
                                    String cls = element.getClassName();
                                    if (cls.startsWith("run.xbud") || cls.startsWith("com.xbud")) {
                                        sb.append(cls).append(".").append(element.getMethodName())
                                                .append(":").append(element.getLineNumber())
                                                .append(" < ");
                                    }
                                }
                                log("IMG " + id + " " + name + " <- " + sb);
                            } catch (Throwable ignored) {
                            }
                        }
                    });
            log("image hook installed");
        } catch (Throwable t) {
            log("image hook failed: " + t);
        }
    }

    private static boolean adapterHooked;

    /** Reveal the requirement rows' data model and who builds it. */
    private static void hookAdapter(ClassLoader loader) {
        if (adapterHooked) {
            return;
        }
        try {
            Class<?> cls = Class.forName(
                    "run.xbud.android.adapter.FinishDialogReasonAdapter", false, loader);
            XposedBridge.hookAllConstructors(cls, new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        StringBuilder sb = new StringBuilder();
                        for (StackTraceElement e : new Throwable().getStackTrace()) {
                            String name = e.getClassName();
                            if (name.startsWith("run.xbud") || name.startsWith("com.xbud")) {
                                sb.append(name).append(".").append(e.getMethodName())
                                        .append(":").append(e.getLineNumber()).append(" < ");
                            }
                        }
                        log("ADAPTER new <- " + sb);
                        if (param.args != null) {
                            for (Object arg : param.args) {
                                log("ADAPTER arg " + (arg == null ? "null" : arg.getClass().getName()));
                                if (arg instanceof List) {
                                    for (Object item : (List<?>) arg) {
                                        logAllFields(item);
                                    }
                                }
                            }
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
            XposedBridge.hookAllMethods(cls, "onBindViewHolder", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        int pos = param.args.length > 1 && param.args[1] instanceof Integer
                                ? (Integer) param.args[1] : -1;
                        log("BIND pos=" + pos);
                        logFieldsWithPreview(param.thisObject);
                    } catch (Throwable ignored) {
                    }
                }
            });
            for (final java.lang.reflect.Method method : cls.getDeclaredMethods()) {
                Class<?>[] types = method.getParameterTypes();
                if (types.length != 1 || !List.class.isAssignableFrom(types[0])) {
                    continue;
                }
                try {
                    final String name = method.getName();
                    XposedBridge.hookAllMethods(cls, name, new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            try {
                                Object arg = param.args[0];
                                log("ADAPTER set " + name + " n="
                                        + (arg instanceof List ? ((List<?>) arg).size() : -1));
                                if (arg instanceof List) {
                                    for (Object item : (List<?>) arg) {
                                        if (item == null) {
                                            log("ADAPTER item null");
                                            continue;
                                        }
                                        log("ADAPTER item " + item.getClass().getName()
                                                + " :: " + trunc(String.valueOf(item), 120));
                                        logAllFields(item);
                                        forceAllBooleans(item);
                                    }
                                }
                            } catch (Throwable ignored) {
                            }
                        }
                    });
                } catch (Throwable ignored) {
                }
            }
            adapterHooked = true;
            log("adapter hooks installed");
            try {
                Class<?> base = Class.forName(
                        "run.xbud.android.mvp.ui.sport.run.BaseRunActivity", false, loader);
                XposedBridge.hookAllMethods(base, "j3", new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        try {
                            log("J3 args=" + (param.args == null ? 0 : param.args.length)
                                    + " ret=" + trunc(String.valueOf(param.getResult()), 400));
                            if (param.args != null) {
                                for (Object arg : param.args) {
                                    log("J3 arg " + (arg == null ? "null"
                                            : arg.getClass().getName()) + " " + trunc(String.valueOf(arg), 300));
                                }
                            }
                        } catch (Throwable ignored) {
                        }
                    }
                });
                log("j3 hooked");
            } catch (Throwable t) {
                log("j3 hook failed: " + t);
            }
        } catch (Throwable t) {
            log("adapter hook failed: " + t);
        }
    }

    private static void logFieldsWithPreview(Object target) {
        if (target == null) {
            return;
        }
        Class<?> type = target.getClass();
        while (type != null && type != Object.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    log("F " + type.getSimpleName() + "." + field.getName() + " = "
                            + preview(field.get(target)));
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
    }

    private static void forceAllBooleans(Object item) {
        if (item == null) {
            return;
        }
        Class<?> type = item.getClass();
        while (type != null && type != Object.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                if (field.getType() != boolean.class) {
                    continue;
                }
                try {
                    field.setAccessible(true);
                    boolean old = field.getBoolean(item);
                    field.setBoolean(item, true);
                    log("FORCEBOOL " + type.getSimpleName() + "." + field.getName()
                            + " " + old + " -> true");
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
    }

    private static boolean beanHooked;

    /** Every completion condition reports true at creation. */
    private static void hookConditionBean(ClassLoader loader) {
        if (beanHooked) {
            return;
        }
        try {
            Class<?> bean = Class.forName(
                    "run.xbud.android.bean.sport.run.CompleteConditionBean", false, loader);
            XposedBridge.hookAllConstructors(bean, new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    try {
                        java.lang.reflect.Field f = param.thisObject.getClass()
                                .getDeclaredField("complete");
                        f.setAccessible(true);
                        f.setBoolean(param.thisObject, true);
                        java.lang.reflect.Field r = param.thisObject.getClass()
                                .getDeclaredField("reason");
                        r.setAccessible(true);
                        log("BEAN " + r.get(param.thisObject) + " complete=true");
                    } catch (Throwable ignored) {
                    }
                }
            });
            beanHooked = true;
            log("condition bean hooked");
        } catch (Throwable t) {
            log("condition bean hook failed: " + t);
        }
    }

    private static final java.util.Set<Class<?>> REASON_CLASSES = new java.util.HashSet<>();

    /** Zero unCompleteReason on the record object before it is serialized/signed. */
    private static void zeroUnCompleteReason(Object target) {
        if (!beanPatch) {
            return;
        }
        Class<?> t = target.getClass();
        while (t != null && t != Object.class) {
            try {
                java.lang.reflect.Field f = t.getDeclaredField("unCompleteReason");
                f.setAccessible(true);
                int old = f.getInt(target);
                if (old != 0) {
                    f.setInt(target, 0);
                    log("RECORD-ZERO " + t.getName() + " " + old + " -> 0");
                }
                return;
            } catch (NoSuchFieldException ignored) {
                t = t.getSuperclass();
            } catch (Throwable ignored) {
                return;
            }
        }
    }

    private static boolean boolHooked;
    private static final java.util.Set<String> BOOL_CLASSES = new java.util.HashSet<>();

    /** Log every zero-arg boolean method: reveals the requirement check. */
    private static void hookBooleanMethods(Class<?> clazz) {
        if (clazz == null || !BOOL_CLASSES.add(clazz.getName())) {
            return;
        }
        boolHooked = true;
        try {
            for (final Method method : clazz.getDeclaredMethods()) {
                if (method.getReturnType() != boolean.class || method.getParameterCount() != 0
                        || java.lang.reflect.Modifier.isStatic(method.getModifiers())) {
                    continue;
                }
                try {
                    final String name = method.getName();
                    XposedBridge.hookAllMethods(clazz, name, new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam param) {
                            log("BOOL " + name + " -> " + param.getResult());
                        }
                    });
                } catch (Throwable ignored) {
                }
            }
            log("bool hooks installed on " + clazz.getName());
        } catch (Throwable t) {
            log("bool hook failed: " + t);
        }
    }

    private static boolean dialogHooked;

    /** Log dialogs and their primitive fields: the finish panel lives here. */
    private static void hookDialogs() {
        if (dialogHooked) {
            return;
        }
        dialogHooked = true;
        try {
            XposedBridge.hookAllMethods(android.app.Dialog.class, "show", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    Object dialog = param.thisObject;
                    if (dialog == null) {
                        return;
                    }
                    log("DIALOG " + dialog.getClass().getName());
                    Class<?> type = dialog.getClass();
                    int depth = 0;
                    while (type != null && type != Object.class && depth < 4) {
                        for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                            if (!field.getType().isPrimitive() && field.getType() != String.class) {
                                continue;
                            }
                            try {
                                field.setAccessible(true);
                                log("DF " + type.getSimpleName() + "." + field.getName()
                                        + " = " + field.get(dialog));
                            } catch (Throwable ignored) {
                            }
                        }
                        type = type.getSuperclass();
                        depth++;
                    }
                }
            });
            log("dialog hook installed");
        } catch (Throwable t) {
            log("dialog hook failed: " + t);
        }
    }

    /** Set every boolean on point-like objects reachable from the activity. */
    private static void forcePassDeep(Activity activity) {
        log("PASS start");
        int hits = 0;
        try {
            Class<?> type = activity.getClass();
        int depth = 0;
        while (type != null && type != Object.class && type != Activity.class && depth < 5) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    Object value = field.get(activity);
                    if (value instanceof List) {
                        List<?> list = (List<?>) value;
                        for (Object item : new ArrayList<Object>(list)) {
                            hits += forcePassObject(item);
                        }
                    } else if (value != null && !field.getType().getName().startsWith("android.")
                            && !field.getType().getName().startsWith("java.")) {
                        hits += forcePassObject(value);
                    }
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
            depth++;
        }
        } catch (Throwable t) {
            log("PASS error " + t);
        }
        log("PASS done hits=" + hits);
    }

    private static int forcePassObject(Object item) {
        if (item == null) {
            return 0;
        }
        String cls = item.getClass().getName();
        String text = String.valueOf(item);
        if (!cls.toLowerCase().contains("point") && !text.contains("pointName")
                && !text.contains("point")) {
            return 0;
        }
        int n = 0;
        Class<?> type = item.getClass();
        while (type != null && type != Object.class) {
            for (java.lang.reflect.Field field : type.getDeclaredFields()) {
                try {
                    field.setAccessible(true);
                    if (field.getType() == boolean.class) {
                        boolean old = field.getBoolean(item);
                        field.setBoolean(item, true);
                        log("PASSFIELD " + cls + "." + field.getName() + " " + old + " -> true");
                        n++;
                    } else if (field.getType() == int.class || field.getType() == long.class) {
                        log("PASSINT " + cls + "." + field.getName() + " = "
                                + field.get(item) + " (type " + field.getType().getSimpleName() + ")");
                    }
                } catch (Throwable ignored) {
                }
            }
            type = type.getSuperclass();
        }
        for (String name : new String[]{"setPass", "setIsPass", "setPassed", "setPassStatus"}) {
            try {
                XposedHelpers.callMethod(item, name, true);
                log("PASSSET " + cls + "." + name + "(true)");
            } catch (Throwable ignored) {
            }
        }
        log("PASSOBJ " + cls + " " + trunc(text, 260));
        return Math.max(n, 1);
    }

    private static void dumpIntent(Activity activity) {
        try {
            android.content.Intent intent = activity.getIntent();
            if (intent == null) {
                return;
            }
            log("INTENT action=" + intent.getAction() + " data=" + intent.getDataString());
            android.os.Bundle extras = intent.getExtras();
            if (extras == null) {
                return;
            }
            for (String key : extras.keySet()) {
                String value = String.valueOf(extras.get(key));
                log("INTENT " + key + "=" + value.substring(0, Math.min(value.length(), 1500)));
            }
        } catch (Throwable t) {
            log("intent dump failed: " + t);
        }
    }

    private static boolean polygonHooked;
    private static volatile Object currentPolygon;

    /** The campus fence is an AMap Polygon; force contains() to true. */
    private static void hookPolygon(Class<?> amap, ClassLoader loader) {
        try {
            XposedBridge.hookAllMethods(amap, "addPolygon", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    Object polygon = param.getResult();
                    if (polygon == null) {
                        return;
                    }
                    currentPolygon = polygon;
                    XC_MethodHook contains = new XC_MethodHook() {
                        @Override
                        protected void afterHookedMethod(MethodHookParam p) {
                            p.setResult(Boolean.TRUE);
                        }
                    };
                    Class<?> type = polygon.getClass();
                    while (type != null && type != Object.class) {
                        XposedBridge.hookAllMethods(type, "contains", contains);
                        type = type.getSuperclass();
                    }
                    if (!polygonHooked) {
                        polygonHooked = true;
                        log("polygon hooked: " + polygon.getClass().getName());
                    }
                }
            });
            log("addPolygon hook installed");
        } catch (Throwable t) {
            log("polygon hook failed: " + t);
        }
    }

    /** Dump the campus fence so routes can be generated inside it. */
    private static void dumpPolygon() {
        Object polygon = currentPolygon;
        if (polygon == null) {
            log("polygon: none captured");
            return;
        }
        try {
            Object points = XposedHelpers.callMethod(polygon, "getPoints");
            if (!(points instanceof List)) {
                log("polygon: no points");
                return;
            }
            StringBuilder sb = new StringBuilder("POLY");
            for (Object item : (List<?>) points) {
                double lat = XposedHelpers.getDoubleField(item, "latitude");
                double lng = XposedHelpers.getDoubleField(item, "longitude");
                sb.append(" ").append(lat).append(",").append(lng);
            }
            log(sb.toString());
        } catch (Throwable t) {
            log("polygon dump failed: " + t);
        }
    }

    private static void dumpExistingMarkers(Object map) {
        try {
            Object markers = XposedHelpers.callMethod(map, "getMapScreenMarkers");
            if (markers instanceof List) {
                for (Object marker : (List<?>) markers) {
                    dumpMarker(marker);
                }
            }
        } catch (Throwable t) {
            log("existing markers unavailable: " + t);
        }
    }

    /** Custom checkpoint pins are positioned through Projection.toScreenLocation. */
    private static void hookProjection(Object map, ClassLoader classLoader) {
        try {
            Object projection = XposedHelpers.callMethod(map, "getProjection");
            if (projection == null) {
                log("projection null");
                return;
            }
            currentProjection = projection;
            XC_MethodHook hook = new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    for (Object arg : param.args) {
                        logLatLng("PROJ", arg);
                    }
                }
            };
            Class<?> type = projection.getClass();
            while (type != null && type != Object.class) {
                XposedBridge.hookAllMethods(type, "toScreenLocation", hook);
                type = type.getSuperclass();
            }
            log("projection hooks installed: " + projection.getClass().getName());
            hookLatLng(classLoader);
        } catch (Throwable t) {
            log("projection hook failed: " + t);
        }
    }

    /** Every AMap coordinate literal goes through new LatLng(lat, lng). */
    private static void hookLatLng(ClassLoader classLoader) {
        try {
            Class<?> latLng = Class.forName(
                    "com.amap.api.maps.model.LatLng", false, classLoader);
            XposedBridge.hookAllConstructors(latLng, new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (latLngLog && param.args != null && param.args.length >= 2) {
                        log("LL " + param.args[0] + "," + param.args[1]);
                    }
                }
            });
            log("LatLng constructor hooked");
        } catch (Throwable t) {
            log("LatLng hook failed: " + t);
        }
    }

    private static void logLatLng(String tag, Object value) {
        try {
            double lat = XposedHelpers.getDoubleField(value, "latitude");
            double lng = XposedHelpers.getDoubleField(value, "longitude");
            log(tag + " " + lat + "," + lng);
        } catch (Throwable ignored) {
        }
    }

    private static void dumpMarker(Object options) {
        if (options == null) {
            return;
        }
        try {
            Object position = XposedHelpers.callMethod(options, "getPosition");
            Object title = XposedHelpers.callMethod(options, "getTitle");
            double lat = XposedHelpers.getDoubleField(position, "latitude");
            double lng = XposedHelpers.getDoubleField(position, "longitude");
            log("MARKER " + title + " " + lat + "," + lng);
        } catch (Throwable ignored) {
        }
    }

    private static void trackActivity() {
        try {
            XposedBridge.hookAllMethods(Activity.class, "onResume", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (param.thisObject instanceof Activity) {
                        Activity activity = (Activity) param.thisObject;
                        currentActivity = activity;
                        hookMapFromActivity(activity);
                        hookFlutter(activity.getClass().getClassLoader());
                        hookGson(activity.getClass().getClassLoader());
                        hookOkHttp(activity.getClass().getClassLoader());
                        hookAmapDistance(activity.getClass().getClassLoader());
                        hookSportJni(activity.getClass().getClassLoader());
                        hookPointModel(activity.getClass().getClassLoader());
                        if (activity.getClass().getName().contains("RunAdvanced")) {
                            dumpIntent(activity);
                            dumpFields(activity);
                        }
                    }
                }
            });
        } catch (Throwable t) {
            log("activity hook failed: " + t);
        }
    }

    private static void startCommandWatcher() {
        if (cmdWatcherStarted) {
            return;
        }
        cmdWatcherStarted = true;
        Thread thread = new Thread(new Runnable() {
            @Override
            public void run() {
                while (true) {
                    try {
                        File file = new File(CMD_FILE);
                        if (file.isFile()) {
                            String text = readText(file).trim();
                            if (!text.isEmpty() && !text.equals(lastCommand)) {
                                lastCommand = text;
                                executeCommand(text);
                            }
                        }
                        Thread.sleep(400L);
                    } catch (Throwable ignored) {
                    }
                }
            }
        }, "XbudLoc-cmd");
        thread.setDaemon(true);
        thread.start();
    }

    private static String readText(File file) throws Exception {
        try (FileInputStream in = new FileInputStream(file)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int n;
            while ((n = in.read(buffer)) > 0) {
                out.write(buffer, 0, n);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private static void executeCommand(final String command) {
        String[] early = command.split("\\s+");
        if ("target".equals(early[0]) && early.length >= 2) {
            String[] pair = early[1].split(",");
            targetLat = Double.parseDouble(pair[0]);
            targetLng = Double.parseDouble(pair[1]);
            targetActive = true;
            orbitActive = false;
            arrivedLogged = false;
            log("TARGET " + targetLat + "," + targetLng);
            return;
        }
        if ("stop".equals(early[0])) {
            targetActive = false;
            orbitActive = false;
            log("TARGET off");
            return;
        }
        if ("reset".equals(early[0])) {
            travelled = 0;
            frozen = false;
            Location first = route.locationAt(0);
            curLat = first.getLatitude();
            curLng = first.getLongitude();
            log("RESET to " + curLat + "," + curLng);
            return;
        }
        if ("hold".equals(early[0])) {
            frozen = true;
            log("HOLD at travelled=" + travelled);
            return;
        }
        if ("go".equals(early[0])) {
            frozen = false;
            log("GO travelled=" + travelled);
            return;
        }
        if ("diststack".equals(early[0])) {
            distStack = !distStack;
            STACKS.clear();
            log("DISTSTACK " + distStack);
            return;
        }
        if ("passhook".equals(early[0])) {
            hookPassSetters();
            return;
        }
        if ("llog".equals(early[0])) {
            latLngLog = !latLngLog;
            log("LLLOG " + latLngLog);
            return;
        }
        if ("beanpatch".equals(early[0])) {
            beanPatch = !beanPatch;
            log("BEANPATCH " + beanPatch);
            return;
        }
        if ("md5hook".equals(early[0])) {
            hookDigest();
            return;
        }
        if ("fields".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity == null) {
                log("fields: no activity");
            } else {
                dumpFieldsDeep(activity);
            }
            return;
        }
        if ("points".equals(early[0])) {
            dumpPointList();
            return;
        }
        if ("polygon".equals(early[0])) {
            dumpPolygon();
            return;
        }
        if ("flip".equals(early[0])) {
            flipPointList();
            return;
        }
        if ("beanhooks".equals(early[0])) {
            hookConditionBean(appLoader);
            return;
        }
        if ("adapterhooks".equals(early[0])) {
            hookAdapter(appLoader);
            return;
        }
        if ("texthooks".equals(early[0])) {
            hookText();
            return;
        }
        if ("imghooks".equals(early[0])) {
            hookImages();
            return;
        }
        if ("views".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity != null) {
                dumpViews(activity);
            }
            return;
        }
        if ("state".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity != null) {
                dumpPointState(activity);
            }
            return;
        }
        if ("boolhooks".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity != null) {
                hookBooleanMethods(activity.getClass());
                ClassLoader loader = appLoader;
                if (loader != null) {
                    String[] names = {
                            "run.xbud.android.mvp.ui.sport.run.BaseRunActivity",
                            "run.xbud.android.mvp.ui.sport.run.RunScoreActivity",
                            "run.xbud.android.mvp.ui.sport.run.BaseUtilsActivity",
                            "run.xbud.android.mvp.ui.sport.run.RunOperateFragment"
                    };
                    for (String name : names) {
                        try {
                            hookBooleanMethods(Class.forName(name, false, loader));
                        } catch (Throwable t) {
                            log("bool hook skip " + name + ": " + t);
                        }
                    }
                }
            }
            return;
        }
        if ("dialoghooks".equals(early[0])) {
            hookDialogs();
            return;
        }
        if ("markers".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity == null) {
                log("markers: no activity");
            } else {
                dumpMarkers(activity);
            }
            return;
        }
        if ("pass".equals(early[0])) {
            Activity activity = currentActivity;
            if (activity == null) {
                log("pass: no activity");
            } else {
                forcePassDeep(activity);
            }
            return;
        }
        if ("speed".equals(early[0]) && early.length >= 2) {
            targetSpeed = Double.parseDouble(early[1]);
            log("SPEED " + targetSpeed);
            return;
        }
        if ("orbit".equals(early[0])) {            orbitCenterLat = curLat;
            orbitCenterLng = curLng;
            orbitAngle = 0;
            if (early.length >= 2) {
                orbitRadius = Double.parseDouble(early[1]);
            }
            orbitActive = true;
            targetActive = false;
            log("ORBIT r=" + orbitRadius + " @" + curLat + "," + curLng);
            return;
        }
        if ("cam".equals(early[0]) && currentMap != null) {
            try {
                Object position = XposedHelpers.callMethod(currentMap, "getCameraPosition");
                Object zoom = XposedHelpers.getObjectField(position, "zoom");
                Object scale = XposedHelpers.callMethod(currentMap, "getScalePerPixel");
                Object latLng = Class.forName("com.amap.api.maps.model.LatLng", false,
                                currentMap.getClass().getClassLoader())
                        .getConstructor(double.class, double.class)
                        .newInstance(curLat, curLng);
                Object point = XposedHelpers.callMethod(
                        currentProjection, "toScreenLocation", latLng);
                Object px = XposedHelpers.getObjectField(point, "x");
                Object py = XposedHelpers.getObjectField(point, "y");
                log("CAM zoom=" + zoom + " scale=" + scale
                        + " screen=" + px + "," + py
                        + " pos=" + curLat + "," + curLng);
            } catch (Throwable t) {
                log("cam failed: " + t);
            }
            return;
        }
        if ("zoom".equals(early[0]) && early.length >= 2 && currentMap != null) {
            try {
                Class<?> factory = Class.forName("com.amap.api.maps.CameraUpdateFactory",
                        false, currentMap.getClass().getClassLoader());
                Object update = XposedHelpers.callStaticMethod(
                        factory, "zoomTo", Float.parseFloat(early[1]));
                XposedHelpers.callMethod(currentMap, "moveCamera", update);
                log("ZOOM " + early[1]);
            } catch (Throwable t) {
                log("zoom failed: " + t);
            }
            return;
        }
        final Activity activity = currentActivity;
        if (activity == null) {
            log("cmd " + command + ": no activity");
            return;
        }
        activity.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    String[] parts = command.split("\\s+");
                    String action = parts[0];
                    if ("project".equals(action) && parts.length >= 3) {
                        Object projection = currentProjection;
                        if (projection == null) {
                            log("no projection yet");
                            return;
                        }
                        float x = Float.parseFloat(parts[1]);
                        float y = Float.parseFloat(parts[2]);
                        Object point = Class.forName("android.graphics.Point")
                                .getConstructor(int.class, int.class)
                                .newInstance((int) x, (int) y);
                        Object latLng = XposedHelpers.callMethod(
                                projection, "fromScreenLocation", point);
                        double lat = XposedHelpers.getDoubleField(latLng, "latitude");
                        double lng = XposedHelpers.getDoubleField(latLng, "longitude");
                        log("PROJFROM " + x + "," + y + " -> " + lat + "," + lng);
                        return;
                    }
                    String name = null;
                    if ("pause".equals(action)) {
                        name = "btnPause";
                    } else if ("resume".equals(action)) {
                        name = "btnContinue";
                    } else if ("finish".equals(action)) {
                        name = "btnFinish";
                    } else if ("click".equals(action) && parts.length > 1) {
                        name = parts[1];
                    }
                    if (name == null) {
                        return;
                    }
                    int id = activity.getResources()
                            .getIdentifier(name, "id", activity.getPackageName());
                    if (id == 0) {
                        log("id missing " + name);
                        return;
                    }
                    View view = activity.findViewById(id);
                    if (view == null) {
                        log("view missing " + name);
                        return;
                    }
                    view.setVisibility(View.VISIBLE);
                    int[] xy = new int[2];
                    view.getLocationOnScreen(xy);
                    log("VIEW " + name + " rect=" + xy[0] + "," + xy[1] + " "
                            + view.getWidth() + "x" + view.getHeight()
                            + " vis=" + view.getVisibility());
                    boolean clicked = view.performClick();
                    boolean longClicked = view.performLongClick();
                    log("cmd " + command + " click=" + clicked + " long=" + longClicked);
                    if ("finish".equals(action)) {
                        // Custom long-press views use touch timing, not OnLongClickListener.
                        dispatchLongPress(view);
                    }
                } catch (Throwable t) {
                    log("cmd failed: " + t);
                }
            }
        });
    }

    private static void dispatchLongPress(final View view) {
        final Handler handler = new Handler(Looper.getMainLooper());
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                try {
                    final int w = Math.max(view.getWidth(), 2);
                    final int h = Math.max(view.getHeight(), 2);
                    final long down = SystemClock.uptimeMillis();
                    MotionEvent press = MotionEvent.obtain(
                            down, down, MotionEvent.ACTION_DOWN, w / 2f, h / 2f, 0);
                    view.dispatchTouchEvent(press);
                    handler.postDelayed(new Runnable() {
                        @Override
                        public void run() {
                            long up = SystemClock.uptimeMillis();
                            MotionEvent release = MotionEvent.obtain(
                                    down, up, MotionEvent.ACTION_UP, w / 2f, h / 2f, 0);
                            view.dispatchTouchEvent(release);
                        }
                    }, 4200L);
                } catch (Throwable ignored) {
                }
            }
        }, 400L);
    }

    private static void hookSensors() {
        try {
            sensorEventCtor = SensorEvent.class.getDeclaredConstructor(int.class);
            sensorEventCtor.setAccessible(true);
        } catch (Throwable t) {
            log("SensorEvent constructor unavailable: " + t);
        }
        try {
            XposedBridge.hookAllMethods(SensorManager.class, "registerListener",
                    new XC_MethodHook() {
                        @Override
                        protected void beforeHookedMethod(MethodHookParam param) {
                            SensorEventListener listener = null;
                            Sensor sensor = null;
                            for (Object arg : param.args) {
                                if (arg instanceof SensorEventListener) {
                                    listener = (SensorEventListener) arg;
                                } else if (arg instanceof Sensor) {
                                    sensor = (Sensor) arg;
                                }
                            }
                            if (listener == null || sensor == null
                                    || sensor.getType() != Sensor.TYPE_ACCELEROMETER) {
                                return;
                            }
                            accelerometer = sensor;
                            registerAccel(listener);
                            for (int i = 0; i < param.args.length; i++) {
                                if (param.args[i] == listener) {
                                    param.args[i] = new AccelFilter(sensor);
                                }
                            }
                        }
                    });
            XposedBridge.hookAllMethods(SensorManager.class, "unregisterListener",
                    new XC_MethodHook() {
                        @Override
                        protected void beforeHookedMethod(MethodHookParam param) {
                            for (Object arg : param.args) {
                                if (arg instanceof SensorEventListener) {
                                    synchronized (ACCEL_LOCK) {
                                        ACCEL_LISTENERS.remove(arg);
                                    }
                                }
                            }
                        }
                    });
            log("sensor hooks installed");
        } catch (Throwable t) {
            log("sensor hook failed: " + t);
        }
    }

    private static void registerAccel(SensorEventListener listener) {
        synchronized (ACCEL_LOCK) {
            if (!ACCEL_LISTENERS.contains(listener)) {
                ACCEL_LISTENERS.add(listener);
            }
            if (!accelRunning) {
                accelRunning = true;
                accelStartNanos = System.nanoTime();
                accelHandler = new Handler(Looper.getMainLooper());
                accelHandler.postDelayed(ACCEL_TICK, 20L);
            }
        }
    }

    private static final Runnable ACCEL_TICK = new Runnable() {
        @Override
        public void run() {
            synchronized (ACCEL_LOCK) {
                if (!accelRunning || sensorEventCtor == null || accelerometer == null) {
                    return;
                }
                double t = (System.nanoTime() - accelStartNanos) / 1e9;
                double phase = 2 * Math.PI * CADENCE_HZ * t;
                float x = (float) (0.9 * Math.sin(phase + 1.2) + (Math.random() - 0.5) * 0.15);
                float y = (float) (0.6 * Math.sin(phase * 0.5) + (Math.random() - 0.5) * 0.15);
                float z = (float) (9.80665 + 3.2 * Math.sin(phase)
                        + 0.6 * Math.sin(2 * phase + 0.7) + (Math.random() - 0.5) * 0.2);
                for (SensorEventListener listener : new ArrayList<>(ACCEL_LISTENERS)) {
                    try {
                        SensorEvent event = sensorEventCtor.newInstance(3);
                        event.sensor = accelerometer;
                        event.accuracy = 3;
                        event.timestamp = System.nanoTime();
                        event.values[0] = x;
                        event.values[1] = y;
                        event.values[2] = z;
                        listener.onSensorChanged(event);
                    } catch (Throwable ignored) {
                        // Keep feeding the remaining listeners.
                    }
                }
                accelHandler.postDelayed(this, 20L);
            }
        }
    };

    /** Swallows the real accelerometer stream for this registration. */
    private static final class AccelFilter implements SensorEventListener {
        private final Sensor sensor;

        AccelFilter(Sensor sensor) {
            this.sensor = sensor;
        }

        @Override
        public void onSensorChanged(SensorEvent event) {
            if (event.sensor != null
                    && event.sensor.getType() == Sensor.TYPE_ACCELEROMETER) {
                return;
            }
        }

        @Override
        public void onAccuracyChanged(Sensor changed, int accuracy) {
        }
    }

    private static void hook(String name, XC_MethodHook callback, Object... types) {
        try {
            Object[] args = new Object[types.length + 1];
            System.arraycopy(types, 0, args, 0, types.length);
            args[types.length] = callback;
            XposedHelpers.findAndHookMethod(LocationManager.class, name, args);
        } catch (Throwable t) {
            log("hook " + name + " skipped: " + t);
        }
    }

    private static LocationListener findListener(Object[] args) {
        if (args == null) {
            return null;
        }
        for (Object arg : args) {
            if (arg instanceof LocationListener) {
                return (LocationListener) arg;
            }
        }
        return null;
    }

    private static void register(LocationListener listener) {
        synchronized (LOCK) {
            if (!LISTENERS.contains(listener)) {
                LISTENERS.add(listener);
            }
            try {
                listener.onProviderEnabled(LocationManager.GPS_PROVIDER);
            } catch (Throwable ignored) {
            }
            if (!running) {
                running = true;
                travelled = 0;
                Location first = route.locationAt(0);
                curLat = first.getLatitude();
                curLng = first.getLongitude();
                handler = new Handler(Looper.getMainLooper());
                handler.postDelayed(TICK, 200L);
            }
            log("REG listeners=" + LISTENERS.size() + " running=" + running);
        }
    }

    private static Location makeLocation(double lat, double lng, double bearing) {
        Location location = new Location(LocationManager.GPS_PROVIDER);
        location.setLatitude(lat);
        location.setLongitude(lng);
        location.setAccuracy(2.0f);
        location.setAltitude(50.0);
        location.setSpeed((float) (targetActive ? targetSpeed : route.speed));
        location.setBearing((float) bearing);
        location.setTime(System.currentTimeMillis());
        location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
        Bundle extras = new Bundle();
        extras.putInt("satellites", 12);
        extras.putInt("satelliteCount", 12);
        location.setExtras(extras);
        return location;
    }

    private static double distance(double lat1, double lng1, double lat2, double lng2) {
        double r = 6371000.0;
        double p1 = Math.toRadians(lat1);
        double p2 = Math.toRadians(lat2);
        double dp = p2 - p1;
        double dl = Math.toRadians(lng2 - lng1);
        double h = Math.sin(dp / 2) * Math.sin(dp / 2)
                + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
        return 2 * r * Math.asin(Math.sqrt(h));
    }

    private static double bearing(double lat1, double lng1, double lat2, double lng2) {
        double p1 = Math.toRadians(lat1);
        double p2 = Math.toRadians(lat2);
        double dl = Math.toRadians(lng2 - lng1);
        double y = Math.sin(dl) * Math.cos(p2);
        double x = Math.cos(p1) * Math.sin(p2)
                - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
        double deg = Math.toDegrees(Math.atan2(y, x));
        return (deg + 360.0) % 360.0;
    }

    private static void log(String message) {
        XposedBridge.log(TAG + ": " + message);
    }

    /** A listener that swallows the real provider stream. */
    private static final class NoopListener implements LocationListener {
        @Override
        public void onLocationChanged(Location location) {
        }

        @Override
        public void onStatusChanged(String provider, int status, Bundle extras) {
        }

        @Override
        public void onProviderEnabled(String provider) {
        }

        @Override
        public void onProviderDisabled(String provider) {
        }
    }

    /** Polyline route with an optional speed profile and a closed loop. */
    private static final class Route {
        final double[] lat;
        final double[] lng;
        final double[] cumulative;
        final double total;
        final double speed;
        final double[] profileDist;
        final double[] profileSpeed;
        double jitterLat;
        double jitterLng;

        private Route(double[] lat, double[] lng, double speed) {
            this(lat, lng, speed, null, null);
        }

        private Route(double[] lat, double[] lng, double speed,
                      double[] profileDist, double[] profileSpeed) {
            this.lat = lat;
            this.lng = lng;
            this.speed = speed;
            this.profileDist = profileDist;
            this.profileSpeed = profileSpeed;
            this.cumulative = new double[lat.length];
            double sum = 0;
            for (int i = 1; i < lat.length; i++) {
                sum += distance(lat[i - 1], lng[i - 1], lat[i], lng[i]);
                cumulative[i] = sum;
            }
            this.total = Math.max(sum, 1.0);
        }

        int size() {
            return lat.length;
        }

        /** Instantaneous ground speed, from the profile when one is supplied. */
        double speedAt(double metres) {
            if (profileDist == null || profileDist.length < 2) {
                return speed;
            }
            double d = ((metres % total) + total) % total;
            int index = 0;
            while (index + 1 < profileDist.length && profileDist[index + 1] <= d) {
                index++;
            }
            if (index + 1 >= profileDist.length) {
                return profileSpeed[profileSpeed.length - 1];
            }
            double span = profileDist[index + 1] - profileDist[index];
            double f = span <= 0 ? 0 : (d - profileDist[index]) / span;
            return profileSpeed[index] + (profileSpeed[index + 1] - profileSpeed[index]) * f;
        }

        Location locationAt(double metres) {
            double d = ((metres % total) + total) % total;
            int index = 1;
            while (index < cumulative.length && cumulative[index] < d) {
                index++;
            }
            int i = Math.min(index, cumulative.length - 1);
            int j = Math.max(i - 1, 0);
            double span = cumulative[i] - cumulative[j];
            double f = span <= 0 ? 0 : (d - cumulative[j]) / span;
            double la = lat[j] + (lat[i] - lat[j]) * f;
            double lo = lng[j] + (lng[i] - lng[j]) * f;
            double bearing = bearing(lat[j], lng[j], lat[i], lng[i]);

            // Realistic GPS behaviour: AR(1) drift, occasional multipath excursions,
            // a speed wave and a drifting accuracy figure.
            jitterLat = jitterLat * 0.85 + (Math.random() - 0.5) * 1.4;
            jitterLng = jitterLng * 0.85 + (Math.random() - 0.5) * 1.4;
            if (Math.random() < 0.03) {
                jitterLat += (Math.random() - 0.5) * 5.0;
                jitterLng += (Math.random() - 0.5) * 5.0;
            }
            double mLat = 1.0 / 111320.0;
            double mLng = 1.0 / (111320.0 * Math.cos(Math.toRadians(la)));
            la += jitterLat * mLat;
            lo += jitterLng * mLng;
            double v = speedAt(metres);
            double wave = 1.0 + 0.09 * Math.sin(metres / 28.0)
                    + 0.05 * Math.sin(metres / 7.0 + 1.1);
            float accuracy = 3.5f + (float) Math.random() * 4.5f;
            if (Math.random() < 0.08) {
                accuracy += 4.0f;
            }

            Location location = new Location(LocationManager.GPS_PROVIDER);
            location.setLatitude(la);
            location.setLongitude(lo);
            location.setAccuracy(accuracy);
            location.setAltitude(48.0 + 4.0 * Math.sin(metres / 220.0)
                    + Math.random() * 0.8);
            location.setSpeed((float) (v * wave * (0.96 + Math.random() * 0.08)));
            location.setBearing((float) (bearing + (Math.random() - 0.5) * 12.0));
            location.setTime(System.currentTimeMillis());
            location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
            Bundle extras = new Bundle();
            extras.putInt("satellites", 12);
            extras.putInt("satelliteCount", 12);
            extras.putFloat("meanCn0", 38.0f);
            location.setExtras(extras);
            return location;
        }

        static Route load() {
            try {
                File file = new File(ROUTE_FILE);
                if (file.isFile()) {
                    String text = read(file);
                    JSONObject json = new JSONObject(text);
                    JSONArray points = json.getJSONArray("points");
                    if (points.length() >= 2) {
                        double[] la = new double[points.length()];
                        double[] lo = new double[points.length()];
                        for (int i = 0; i < points.length(); i++) {
                            JSONArray pair = points.getJSONArray(i);
                            la[i] = pair.getDouble(0);
                            lo[i] = pair.getDouble(1);
                        }
                        double[] pd = null;
                        double[] ps = null;
                        JSONArray profile = json.optJSONArray("profile");
                        if (profile != null && profile.length() >= 2) {
                            pd = new double[profile.length()];
                            ps = new double[profile.length()];
                            for (int i = 0; i < profile.length(); i++) {
                                JSONArray pair = profile.getJSONArray(i);
                                pd[i] = pair.getDouble(0);
                                ps[i] = pair.getDouble(1);
                            }
                        }
                        return new Route(la, lo, json.optDouble("speed", 3.0), pd, ps);
                    }
                }
            } catch (Throwable t) {
                XposedBridge.log(TAG + ": route file ignored: " + t);
            }
            Route fallback = stadium(33.552897, 119.041092, 400.0, 60.0, 3.0);
            log("ROUTE default first=" + fallback.locationAt(0).getLatitude()
                    + "," + fallback.locationAt(0).getLongitude());
            return fallback;
        }

        private static String read(File file) throws Exception {
            try (FileInputStream in = new FileInputStream(file)) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buffer = new byte[8192];
                int n;
                while ((n = in.read(buffer)) > 0) {
                    out.write(buffer, 0, n);
                }
                return new String(out.toByteArray(), StandardCharsets.UTF_8);
            }
        }

        /** 400 m stadium with two straights and two semicircles. */
        static Route stadium(double lat0, double lng0, double perimeter,
                             double width, double speed) {
            double radius = width / 2.0;
            double straight = (perimeter - 2.0 * Math.PI * radius) / 2.0;
            double mLat = 1.0 / 111320.0;
            double mLng = 1.0 / (111320.0 * Math.cos(Math.toRadians(lat0)));
            List<double[]> points = new ArrayList<>();

            int straightSteps = Math.max(2, (int) Math.round(straight / 8.0));
            int arcSteps = Math.max(4, (int) Math.round(Math.PI * radius / 8.0));

            for (int i = 0; i < straightSteps; i++) {
                double x = -straight / 2.0 + straight * i / straightSteps;
                points.add(new double[]{lat0 - radius * mLat, lng0 + x * mLng});
            }
            for (int i = 0; i < arcSteps; i++) {
                double a = -Math.PI / 2.0 + Math.PI * i / arcSteps;
                points.add(new double[]{
                        lat0 + radius * Math.sin(a) * mLat,
                        lng0 + (straight / 2.0 + radius * Math.cos(a)) * mLng});
            }
            for (int i = 0; i < straightSteps; i++) {
                double x = straight / 2.0 - straight * i / straightSteps;
                points.add(new double[]{lat0 + radius * mLat, lng0 + x * mLng});
            }
            for (int i = 0; i < arcSteps; i++) {
                double a = Math.PI / 2.0 + Math.PI * i / arcSteps;
                points.add(new double[]{
                        lat0 + radius * Math.sin(a) * mLat,
                        lng0 + (-straight / 2.0 + radius * Math.cos(a)) * mLng});
            }
            points.add(points.get(0));

            double[] la = new double[points.size()];
            double[] lo = new double[points.size()];
            for (int i = 0; i < points.size(); i++) {
                la[i] = points.get(i)[0];
                lo[i] = points.get(i)[1];
            }
            return new Route(la, lo, speed);
        }

        private static double distance(double lat1, double lng1, double lat2, double lng2) {
            double r = 6371000.0;
            double p1 = Math.toRadians(lat1);
            double p2 = Math.toRadians(lat2);
            double dp = p2 - p1;
            double dl = Math.toRadians(lng2 - lng1);
            double h = Math.sin(dp / 2) * Math.sin(dp / 2)
                    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
            return 2 * r * Math.asin(Math.sqrt(h));
        }

        private static double bearing(double lat1, double lng1, double lat2, double lng2) {
            double p1 = Math.toRadians(lat1);
            double p2 = Math.toRadians(lat2);
            double dl = Math.toRadians(lng2 - lng1);
            double y = Math.sin(dl) * Math.cos(p2);
            double x = Math.cos(p1) * Math.sin(p2)
                    - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
            double deg = Math.toDegrees(Math.atan2(y, x));
            return (deg + 360.0) % 360.0;
        }
    }
}
