const SAMPLE_RATE = 16000;
const SAMPLE_SECONDS = 4;


function pcmToWav(frames) {
    const sampleCount = frames.reduce((total, frame) => total + frame.length, 0);
    const buffer = new ArrayBuffer(44 + sampleCount * 2);
    const view = new DataView(buffer);
    const writeText = (offset, text) => {
        for (let index = 0; index < text.length; index += 1) {
            view.setUint8(offset + index, text.charCodeAt(index));
        }
    };
    writeText(0, 'RIFF');
    view.setUint32(4, 36 + sampleCount * 2, true);
    writeText(8, 'WAVE');
    writeText(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, 'data');
    view.setUint32(40, sampleCount * 2, true);
    let offset = 44;
    for (const frame of frames) {
        for (const sample of frame) {
            view.setInt16(offset, sample, true);
            offset += 2;
        }
    }
    return new Blob([buffer], {type: 'audio/wav'});
}


export async function recordVoiceprintSample(onProgress) {
    window.dispatchEvent(new CustomEvent('newtalk:voiceprint-recording'));
    const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
            channelCount: 1,
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
        },
    });
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    const context = new AudioContextClass({latencyHint: 'interactive'});
    const frames = [];
    let node = null;
    let source = null;
    let timer = null;
    try {
        await context.audioWorklet.addModule('/mic-recorder-worklet.js');
        source = context.createMediaStreamSource(stream);
        node = new AudioWorkletNode(context, 'newtalk-mic-recorder');
        node.port.onmessage = (event) => {
            if (event.data?.type === 'audio_frame') {
                frames.push(new Int16Array(event.data.buffer));
                onProgress?.(Math.min(1, frames.length * 320 / (SAMPLE_RATE * SAMPLE_SECONDS)));
            }
        };
        source.connect(node);
        node.connect(context.destination);
        await context.resume();
        await new Promise((resolve) => {
            timer = window.setTimeout(resolve, SAMPLE_SECONDS * 1000);
        });
    } finally {
        if (timer) window.clearTimeout(timer);
        node?.disconnect();
        source?.disconnect();
        for (const track of stream.getTracks()) track.stop();
        await context.close();
    }
    const expectedFrames = SAMPLE_RATE * 3;
    const captured = frames.reduce((total, frame) => total + frame.length, 0);
    if (captured < expectedFrames) {
        throw new Error('有效录音不足 3 秒，请靠近麦克风重新录制。');
    }
    return pcmToWav(frames);
}
