FROM ubuntu:22.04
WORKDIR /app
COPY . .
RUN cd src && bash bkg.sh
