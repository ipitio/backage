FROM ubuntu:22.04
WORKDIR /app
COPY . .
RUN bash src/lib/util.sh
CMD ["bash"]
