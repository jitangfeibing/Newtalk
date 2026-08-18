# Silero VAD model

`silero_vad.onnx` is copied without modification from Silero VAD `v6.2.1`:

- Source: <https://github.com/snakers4/silero-vad/tree/v6.2.1>
- Model: `src/silero_vad/data/silero_vad.onnx`
- License: MIT
- SHA-256: `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`

The model is pinned in the repository so Newtalk starts reproducibly without a
runtime model download. Newtalk uses ONNX Runtime directly and does not install the
PyTorch-based `silero-vad` Python package.
