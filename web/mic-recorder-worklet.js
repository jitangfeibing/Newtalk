class NewtalkMicRecorder extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetSampleRate = 16000;
        this.frameSamples = 320;
        this.ratio = sampleRate / this.targetSampleRate;
        this.source = [];
        this.sourcePosition = 0;
        this.frame = new Int16Array(this.frameSamples);
        this.frameOffset = 0;
    }

    process(inputs, outputs) {
        const output = outputs[0]?.[0];
        if (output) output.fill(0);

        const input = inputs[0]?.[0];
        if (!input?.length) return true;
        for (const sample of input) this.source.push(sample);

        while (this.sourcePosition + 1 < this.source.length) {
            const left = Math.floor(this.sourcePosition);
            const fraction = this.sourcePosition - left;
            const value = this.source[left] * (1 - fraction) + this.source[left + 1] * fraction;
            const clipped = Math.max(-1, Math.min(1, value));
            this.frame[this.frameOffset++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
            this.sourcePosition += this.ratio;

            if (this.frameOffset === this.frameSamples) {
                const buffer = this.frame.buffer;
                this.port.postMessage({type: 'audio_frame', buffer}, [buffer]);
                this.frame = new Int16Array(this.frameSamples);
                this.frameOffset = 0;
            }
        }

        // Keep one source sample so interpolation remains continuous across worklet blocks.
        const consumed = Math.min(
            Math.floor(this.sourcePosition),
            Math.max(0, this.source.length - 1),
        );
        if (consumed > 0) {
            this.source = this.source.slice(consumed);
            this.sourcePosition -= consumed;
        }
        return true;
    }
}

registerProcessor('newtalk-mic-recorder', NewtalkMicRecorder);
