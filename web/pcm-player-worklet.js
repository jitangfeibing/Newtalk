class NewtalkPcmPlayer extends AudioWorkletProcessor {
    constructor() {
        super();
        this.reset();
        this.port.onmessage = (event) => {
            if (event.data.type === 'reset') {
                this.reset();
                return;
            }
            if (event.data.type === 'audio') {
                this.enqueue(event.data.buffer);
                return;
            }
            if (event.data.type === 'play') {
                this.playing = true;
                return;
            }
            if (event.data.type === 'end') this.ended = true;
        };
    }

    reset() {
        this.queue = [];
        this.offset = 0;
        this.playing = false;
        this.ended = false;
        this.started = false;
        this.drained = false;
    }

    enqueue(buffer) {
        const view = new DataView(buffer);
        const samples = new Float32Array(buffer.byteLength / 2);
        for (let index = 0; index < samples.length; index += 1) {
            samples[index] = view.getInt16(index * 2, true) / 32768;
        }
        this.queue.push(samples);
    }

    nextSample() {
        while (this.queue.length > 0) {
            const chunk = this.queue[0];
            if (this.offset < chunk.length) return chunk[this.offset++];
            this.queue.shift();
            this.offset = 0;
        }
        return null;
    }

    process(inputs, outputs) {
        const output = outputs[0];
        for (const channel of output) channel.fill(0);
        if (!this.playing) return true;

        for (let index = 0; index < output[0].length; index += 1) {
            const sample = this.nextSample();
            if (sample === null) {
                if (this.ended && !this.drained) {
                    this.drained = true;
                    this.port.postMessage({type: 'playback_drained'});
                }
                break;
            }
            if (!this.started) {
                this.started = true;
                this.port.postMessage({type: 'playback_started'});
            }
            for (const channel of output) channel[index] = sample;
        }
        return true;
    }
}

registerProcessor('newtalk-pcm-player', NewtalkPcmPlayer);
