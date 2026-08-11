"""本地向量嵌入：fastembed 加载 Hugging Face 小模型，ONNX 推理，不依赖 PyTorch。"""

import logging
import os
import shutil
import sys
from pathlib import Path

import core  # user_data_dir：模型缓存放用户目录，重编译不丢

logger = logging.getLogger(__name__)


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


def _ensure_bundled_model(cache):
    """frozen 且包内带模型时，把模型注入用户缓存目录（幂等；失败静默降级联网下载）。

    fastembed 需要可写的 cache_dir（HF hub 布局 models--<org>--<name>/...），
    而 _MEIPASS 内模型在重装/升级时会被覆盖、路径也不应被写入 →
    首启把包内模型拷到用户 models 目录，之后一直从用户目录加载。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return  # 开发模式：行为不变（首启联网下载）
    src = Path(meipass) / "models" / "fastembed"
    if not src.is_dir():
        return  # 旧版包（未带模型）：行为不变
    try:
        for entry in src.iterdir():
            if not entry.name.startswith("models--"):
                continue
            dst = Path(cache) / entry.name
            if dst.exists():
                continue  # 已有（上次注入/用户自行放置）：不动
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(entry, dst)
            logger.info("已从包内注入嵌入模型 %s → %s", entry.name, dst)
    except Exception:
        # 磁盘满/权限问题等：不阻断启动，fastembed 回退联网下载
        logger.warning("注入包内嵌入模型失败，将回退联网下载", exc_info=True)


def default_embedder(cfg, data_dir):
    """模型缓存目录：HB_MODELS_DIR 覆盖 > 用户数据目录（持久化，重编译不丢）。"""
    cache = os.environ.get("HB_MODELS_DIR")
    if not cache:
        cache = str(core.user_data_dir() / "models")
    _ensure_bundled_model(cache)
    return Embedder(
        model_name=cfg.get("embedding_model", "BAAI/bge-small-zh-v1.5"),
        cache_dir=cache,
        enabled=bool(cfg.get("embedding_enabled", True)),
    )
