FROM ubuntu:22.04
WORKDIR /app
COPY src/lib/setup.sh .
RUN . setup.sh && verify_deps
