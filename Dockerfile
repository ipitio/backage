FROM ubuntu:22.04
WORKDIR /app
COPY src src
RUN cd src && bash bkg.sh
