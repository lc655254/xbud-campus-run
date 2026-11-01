package de.robv.android.xposed;

/** Compile-time stub; the real implementation is provided by LSPosed. */
public final class XposedHelpers {
    public static XC_MethodHook.Unhook findAndHookMethod(
            Class<?> clazz, String methodName, Object... parameterTypesAndCallback) {
        return null;
    }

    public static Class<?> findClass(String className, ClassLoader classLoader) {
        return null;
    }

    public static Object getObjectField(Object obj, String fieldName) {
        return null;
    }

    public static void setObjectField(Object obj, String fieldName, Object value) {
    }

    public static Object callMethod(Object obj, String methodName, Object... args) {
        return null;
    }

    public static double getDoubleField(Object obj, String fieldName) {
        return 0.0;
    }

    public static Object callStaticMethod(Class<?> clazz, String methodName, Object... args) {
        return null;
    }
}
