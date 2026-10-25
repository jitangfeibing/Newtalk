// 所有差值都落在浏览器的单调时钟上，不把服务端墙钟与 performance.now 相减。
export function captureOriginMs(frame, context, now) {
    if (![frame.audio_end_ms, frame.frame_end_time, context.currentTime, now].every(Number.isFinite)) return null;
    return now - (context.currentTime - frame.frame_end_time) * 1000 - frame.audio_end_ms;
}

export function outputPerformanceTime(context, renderTime, now) {
    const stamp = context.getOutputTimestamp?.();
    if (Number.isFinite(renderTime) && Number.isFinite(stamp?.contextTime)
        && Number.isFinite(stamp?.performanceTime) && stamp.performanceTime > 0) {
        return {atMs: stamp.performanceTime + (renderTime - stamp.contextTime) * 1000,
            clock: 'output_timestamp'};
    }
    // 不支持设备输出时间戳时明确降级为渲染时钟，不能声称已经从扬声器发声。
    return {atMs: Number.isFinite(renderTime) && Number.isFinite(context.currentTime)
        ? now + (renderTime - context.currentTime) * 1000 : now, clock: 'render_clock'};
}
