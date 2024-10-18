FROM ubuntu:22.04
WORKDIR /app
COPY src/lib/util.sh .
RUN . util.sh
