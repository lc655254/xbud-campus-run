// 小步点校园跑：江苏电子信息职业学院操场 400m 跑道循环
// FakeLocation -> 脚本 -> 选择本脚本 -> 运行
// 速度 3.0 m/s = 5'33"/km，步频 180 spm，GNSS 卫星视图开启。

// WGS84 centre (GCJ02 33.551549,119.046629 converted for the GNSS feed).
var CENTER = { lat: 33.552897, lng: 119.041092 };
var PERIMETER = 400;   // 米/圈
var WIDTH = 60;        // 跑道宽度（两个半圆的直径）
var SPEED = 3.0;       // m/s
var SPM = 180;         // 步/分

function stadium(lat0, lng0, perimeter, width) {
    var r = width / 2;
    var s = (perimeter - 2 * Math.PI * r) / 2;
    var mLat = 1 / 111320;
    var mLng = 1 / (111320 * Math.cos(lat0 * Math.PI / 180));
    var pts = [];
    var step = 8;
    var ns = Math.max(2, Math.round(s / step));
    var na = Math.max(4, Math.round(Math.PI * r / step));
    var i, a;

    for (i = 0; i < ns; i++) {
        pts.push([lat0 - r * mLat, lng0 + (-s / 2 + s * i / ns) * mLng]);
    }
    for (i = 0; i < na; i++) {
        a = -Math.PI / 2 + Math.PI * i / na;
        pts.push([lat0 + r * Math.sin(a) * mLat, lng0 + (s / 2 + r * Math.cos(a)) * mLng]);
    }
    for (i = 0; i < ns; i++) {
        pts.push([lat0 + r * mLat, lng0 + (s / 2 - s * i / ns) * mLng]);
    }
    for (i = 0; i < na; i++) {
        a = Math.PI / 2 + Math.PI * i / na;
        pts.push([lat0 + r * Math.sin(a) * mLat, lng0 + (-s / 2 + r * Math.cos(a)) * mLng]);
    }
    pts.push(pts[0]);
    return pts;
}

on('start', function () {
    loc.setAccuracy(2.0);
    loc.setAltitude(50.0);
    loc.setReportInterval(500);
    loc.setJitter(true);

    var path = stadium(CENTER.lat, CENTER.lng, PERIMETER, WIDTH);
    route.setPath(path, { smooth: true, samples: 4000 });
    route.setLoopMode('loop');
    route.start(SPEED);

    step.setSpm(SPM);
    gnss.enable(true);

    log('campus route: ' + path.length + ' pts, ' + PERIMETER + 'm/lap, ' +
        SPEED + ' m/s, ' + SPM + ' spm');
});

on('pathDone', function () {
    log('lap done, progress=' + JSON.stringify(route.progress()));
});

on('stop', function () {
    log('campus route stopped');
});
