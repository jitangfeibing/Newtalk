FROM python:3.11-slim

WORKDIR /service

COPY services/voiceprint/pyproject.toml ./pyproject.toml
COPY services/voiceprint/src ./src

ARG VOICEPRINT_EXTRAS=""
RUN if [ -n "$VOICEPRINT_EXTRAS" ]; then \
        python -m pip install --no-cache-dir ".[${VOICEPRINT_EXTRAS}]"; \
    else \
        python -m pip install --no-cache-dir .; \
    fi

EXPOSE 8010
CMD ["newtalk-voiceprint"]
