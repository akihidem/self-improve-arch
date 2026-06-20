# self-improve ループの実行 image（deploy/k8s/job.yaml が参照）。
#
# ビルド: リポジトリルートを context にする
#   docker build -t ghcr.io/akihidem/self-improve-arch:latest -f deploy/Dockerfile .
#
# 候補は untrusted コードとして実行される。image はテスト床に要る pytest を含むが、
# 実行時の隔離は K8s（securityContext + NetworkPolicy）と `--isolation` が担う
# （image 自体は隔離装置ではない）。対象が numpy 等を要るなら本 image を継いで足すこと。
FROM python:3.12-slim

# pytest = テスト床（候補の正しさ検査）に必須。root のうちに入れる。
RUN pip install --no-cache-dir pytest

# skeleton 一式をコピー。job.yaml は WORKDIR の run.py を起動する。
WORKDIR /app
COPY skeleton/ /app/

# 非 root で実行（job.yaml の runAsNonRoot=true / runAsUser=65534 と整合）。
USER 65534:65534

ENV PYTHONDONTWRITEBYTECODE=1

# 既定の kb は読取専用 rootfs では書けないので、運用では --kb-path=/out/... を渡すこと
# （job.yaml がそうしている）。候補/target は --candidates-dir=/in/... を mount で与える。
ENTRYPOINT ["python", "run.py"]
CMD ["--help"]
