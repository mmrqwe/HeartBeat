"""本地向量嵌入：fastembed 加载 Hugging Face 小模型，ONNX 推理，不依赖 PyTorch。"""

import os
from pathlib import Path

import core  # user_data_dir：模型缓存放用户目录，重编译不丢


class Embedder:
    """懒加载嵌入模型；模型缺失或失败时自动降级（返回 None，不影响主流程）。"""

    def __init__(self, model_name="BAAI/bge-small-zh-v1.5", cache_dir=None, enabled=True):
        self.model_name = model_name
        self.cache_dir = str(cache_dir) if cache_dir else None
        self.enabled = enabled
        self._model = None
        self._failed = False

    @property
    def ready(self):
        return self._ensure()

    def _ensure(self):
        if self._model is not None:
            return True
        if self._failed or not self.enabled:
            return False
        try:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=self.cache_dir,
            )
            return True
        except Exception:
            self._failed = True
            return False

    def embed_one(self, text):
        """返回 512 维 float 列表；失败返回 None。"""
        if not text or not self.ready:
            return None
        try:
            vector = next(self._model.embed([text[:1000]]))
            return [float(x) for x in vector]
        except Exception:
            return None

    def embed_many(self, texts):
        if not self.ready:
            return []
        try:
            return [
                [float(x) for x in vector]
                for vector in self._model.embed([t[:1000] for t in texts])
            ]
        except Exception:
            return []


def default_embedder(cfg, data_dir):
    """模型缓存目录：HB_MODELS_DIR 覆盖 > 用户数据目录（持久化，重编译不丢）。"""
    cache = os.environ.get("HB_MODELS_DIR")
    if not cache:
        cache = str(core.user_data_dir() / "models")
    return Embedder(
        model_name=cfg.get("embedding_model", "BAAI/bge-small-zh-v1.5"),
        cache_dir=cache,
        enabled=bool(cfg.get("embedding_enabled", True)),
    )
