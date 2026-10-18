# Frigate 人体检测模型

`yolov9-s-320.onnx`：YOLOv9-s，输入 320x320，Frigate 0.18 官方文档推荐的 OpenVINO 模型（CPU 可跑）。
权重来自 https://github.com/WongKinYiu/yolov9 （GPL-3.0）。

重新导出（在本目录执行，需要 Docker，会下载 YOLOv9 代码和依赖）：

```sh
docker build . --build-arg MODEL_SIZE=s --build-arg IMG_SIZE=320 --output . -f- <<'EOF'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /bin/
WORKDIR /yolov9
ADD https://github.com/WongKinYiu/yolov9.git .
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnx-simplifier==0.4.* onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
EOF
```

（命令原样来自 Frigate v0.18.0 文档 `docs/data/object_detectors_models.yaml`，只把 MODEL_SIZE 改成了 s。）
