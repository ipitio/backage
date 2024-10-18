FROM ubuntu:22.04
WORKDIR /app
COPY src src
RUN bash src/bkg.sh
CMD ["tail", "-f", "/dev/null"]
