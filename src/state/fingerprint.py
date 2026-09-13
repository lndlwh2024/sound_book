import hashlib
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

def compute_text_hash(text: str) -> str:
    """
    计算文本的 SHA256 哈希值。
    仅依据文本本身，用于唯一标识文本内容。
    """
    if not isinstance(text, str):
        text = str(text)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def compute_fingerprint(text_hash: str, backend: str, voice: str, speed: float, options: Optional[Dict[str, Any]] = None) -> str:
    """
    计算 Chunk 的指纹，结合了文本和具体的合成参数，用于缓存命中判断。
    通过指纹可以判断如果参数发生改变，需要重新生成音频。
    """
    fingerprint_dict = {
        "text_hash": text_hash,
        "backend": backend,
        "voice": voice,
        "speed": speed
    }
    
    if options:
        # 将 options 键值对按字母排序加入，保证同样的配置哈希一致
        for k in sorted(options.keys()):
            fingerprint_dict[k] = options[k]
            
    # 序列化为 JSON 字符串并计算 SHA256
    fingerprint_str = json.dumps(fingerprint_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(fingerprint_str.encode('utf-8')).hexdigest()
