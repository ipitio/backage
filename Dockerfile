FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN cd src && bash bkg.sh && rm -rf /var/lib/apt/lists/*
