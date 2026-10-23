// Classic Melt for FFglitch 0.10.2's motion-vector API.
// Only the bridge is modified. The recovery I-frame is left untouched.
let index = 0;
let options;
export function setup(args) {
    args.features = ["mv"];
    options = args.params;
}
function level(t) {
    t *= 100000; // FFedit's parameter JSON accepts integers only.
    const points = options.curve;
    for (let i = 1; i < points.length; i++) {
        if (t <= points[i][0]) {
            const a = points[i - 1], b = points[i];
            return (a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])) / 100000;
        }
    }
    return points[points.length - 1][1] / 100000;
}
export function glitch_frame(frame) {
    const n = index++;
    const range = options.ranges.find(r => n >= r.start && n < r.end);
    const vectors = frame.mv?.forward;
    if (!vectors || !range) return;
    frame.mv.overflow = "truncate";
    const amount = options.strength / 100000 * level(n / Math.max(1, options.total - 1));
    const phase = (n - range.start) / Math.max(1, range.end - range.start);
    const envelope = Math.sin(Math.PI * Math.min(.98, phase + .1));
    for (let y = 0; y < vectors.length; y++) {
        const row = vectors[y];
        for (let x = 0; x < row.length; x++) {
            const mv = row[x];
            const dx = mv[0] * (1 + amount * 2) + amount * 5 * envelope;
            const dy = mv[1] * (1 + amount * 2) - amount * 3 * envelope;
            mv[0] = Math.round(dx);
            mv[1] = Math.round(dy);
        }
    }
}
