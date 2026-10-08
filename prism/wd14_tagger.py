"""
WD14 自动打标签模块
基于 pythongosssss/ComfyUI-WD14-Tagger 的核心实现
模型：SmilingWolf/wd-v1-4-swinv2-tagger-v2 (ONNX)
"""
import os
import logging
import csv
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import numpy as np
from PIL import Image, ImageOps

# onnxruntime 延迟导入：它的 C++ 扩展在某些 Windows 环境会 access violation，
# 放在顶部的话 import 本模块就崩，整 Prism 跟着死。
# 挪到 load_model() 里按需导入，崩了也只影响 AI 打标签功能。

logger = logging.getLogger(__name__)

# 模型配置
MODEL_FILENAME = "model.onnx"
TAGS_FILENAME = "selected_tags.csv"
MODEL_REPO = "SmilingWolf/wd-v1-4-swinv2-tagger-v2"
INPUT_SIZE = 448

# 默认置信度阈值
DEFAULT_GENERAL_THRESHOLD = 0.35
DEFAULT_CHARACTER_THRESHOLD = 0.85

# 下载源（优先使用国内镜像）
HF_MIRRORS = [
    "https://hf-mirror.com",
    "https://huggingface.co"
]


def get_model_dir() -> Path:
    """获取模型存放目录：优先用内置的 assets/ai/，不存在才回退到用户目录"""
    # 1. 优先检查内置模型（随应用分发）
    builtin_dir = Path(__file__).parent / 'assets' / 'ai'
    if builtin_dir.exists():
        builtin_model = builtin_dir / MODEL_FILENAME
        builtin_tags = builtin_dir / TAGS_FILENAME
        if builtin_model.exists() and builtin_tags.exists():
            logger.debug(f"使用内置模型目录: {builtin_dir}")
            return builtin_dir
    
    # 2. 回退到用户目录（兼容旧版或手动放置）
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    else:
        base = Path.home() / '.local' / 'share'
    
    model_dir = base / 'Prism' / 'models' / 'wd14'
    model_dir.mkdir(parents=True, exist_ok=True)
    logger.debug(f"使用用户模型目录: {model_dir}")
    return model_dir


def is_model_available() -> bool:
    """检查模型文件是否存在"""
    model_dir = get_model_dir()
    model_path = model_dir / MODEL_FILENAME
    tags_path = model_dir / TAGS_FILENAME
    return model_path.exists() and tags_path.exists()


def download_model(progress_callback=None) -> bool:
    """
    下载 WD14 模型文件
    
    Args:
        progress_callback: 进度回调函数，签名 callback(current_file, total_files)
        
    Returns:
        是否下载成功
    """
    import urllib.request
    import socket
    
    logger.info("=" * 60)
    logger.info("开始下载 WD14 模型")
    
    model_dir = get_model_dir()
    model_path = model_dir / MODEL_FILENAME
    tags_path = model_dir / TAGS_FILENAME
    
    logger.info(f"模型目录: {model_dir}")
    logger.info(f"模型文件: {model_path}")
    logger.info(f"标签文件: {tags_path}")
    
    # 如果已经存在且大小合理，直接返回成功
    if model_path.exists() and tags_path.exists():
        # 验证文件大小（ONNX 模型通常 > 10MB，CSV 标签 > 10KB）
        if model_path.stat().st_size > 10_000_000 and tags_path.stat().st_size > 10_000:
            logger.info("模型文件已存在且大小合理，跳过下载")
            return True
        else:
            logger.warning("模型文件存在但大小异常，重新下载")
    
    files_to_download = [
        (MODEL_FILENAME, model_path, 40_000_000),  # 预估 40MB
        (TAGS_FILENAME, tags_path, 50_000)         # 预估 50KB
    ]
    
    total_files = len(files_to_download)
    
    for file_idx, (filename, dest_path, estimated_size) in enumerate(files_to_download):
        # 检查现有文件是否有效
        if dest_path.exists():
            min_size = 10_000_000 if filename == MODEL_FILENAME else 10_000
            if dest_path.stat().st_size > min_size:
                logger.info(f"文件已存在且有效，跳过: {filename}")
                if progress_callback:
                    progress_callback(file_idx + 1, total_files)
                continue
        
        # 尝试所有镜像源
        downloaded = False
        for mirror in HF_MIRRORS:
            url = f"{mirror}/{MODEL_REPO}/resolve/main/{filename}"
            logger.info(f"正在下载 {filename} 从 {mirror}...")
            logger.info(f"URL: {url}")
            
            try:
                # 下载到临时文件
                temp_path = dest_path.with_suffix('.tmp')
                
                # 设置超时（30秒连接超时）
                # urlopen supplies its own timeout; do not change global sockets.
                
                # 使用 urlopen 获取响应，支持进度追踪
                logger.info(f"发起 HTTP 请求...")
                response = urllib.request.urlopen(url, timeout=30)
                total_size = int(response.headers.get('Content-Length', estimated_size))
                logger.info(f"响应成功，总大小: {total_size} bytes ({total_size/1024/1024:.2f} MB)")
                
                # 手动下载并追踪进度
                downloaded_size = 0
                chunk_size = 8192
                
                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        # 每下载 1MB 或每 5% 报告一次进度
                        if downloaded_size % (1024 * 1024) < chunk_size or \
                           (total_size > 0 and downloaded_size % (total_size // 20) < chunk_size):
                            progress_mb = downloaded_size / 1024 / 1024
                            total_mb = total_size / 1024 / 1024
                            logger.debug(f"下载进度: {progress_mb:.2f} / {total_mb:.2f} MB ({downloaded_size/total_size*100:.1f}%)")
                
                logger.info(f"下载完成，验证文件大小...")
                
                # 验证文件大小
                min_size = 10_000_000 if filename == MODEL_FILENAME else 10_000
                actual_size = temp_path.stat().st_size
                logger.info(f"实际大小: {actual_size} bytes ({actual_size/1024/1024:.2f} MB)")
                
                if actual_size < min_size:
                    logger.warning(f"下载的文件过小: {actual_size} bytes < {min_size} bytes")
                    temp_path.unlink()
                    continue
                
                # 重命名为最终文件
                temp_path.rename(dest_path)
                
                logger.info(f"下载成功: {filename} ({dest_path.stat().st_size} bytes)")
                downloaded = True
                break
                
            except socket.timeout:
                logger.warning(f"从 {mirror} 下载超时（30秒）")
                temp_path = dest_path.with_suffix('.tmp')
                if temp_path.exists():
                    temp_path.unlink()
                continue
            except Exception as e:
                logger.warning(f"从 {mirror} 下载失败: {type(e).__name__}: {e}", exc_info=True)
                # 清理临时文件
                temp_path = dest_path.with_suffix('.tmp')
                if temp_path.exists():
                    temp_path.unlink()
                continue
        
        if not downloaded:
            logger.error(f"无法下载 {filename}，所有镜像源都失败")
            return False
        
        if progress_callback:
            progress_callback(file_idx + 1, total_files)
    
    # 验证下载结果
    if model_path.exists() and tags_path.exists():
        if model_path.stat().st_size > 10_000_000 and tags_path.stat().st_size > 10_000:
            logger.info("模型下载完成")
            return True
        else:
            logger.error("模型下载后验证失败：文件大小异常")
            return False
    else:
        logger.error("模型下载后验证失败：文件不存在")
        return False


class WD14Tagger:
    """WD14 打标签器 - 基于 pythongosssss/ComfyUI-WD14-Tagger"""
    
    def __init__(self):
        self.model = None
        self.tags = []
        self.general_index = None
        self.character_index = None
        self._loaded = False
    
    def load_model(self) -> bool:
        """加载 ONNX 模型和标签文件"""
        if self._loaded:
            logger.debug("模型已加载，跳过")
            return True
        
        logger.info("=" * 60)
        logger.info("开始加载 WD14 模型")
        
        try:
            model_dir = get_model_dir()
            model_path = model_dir / MODEL_FILENAME
            tags_path = model_dir / TAGS_FILENAME
            
            logger.info(f"模型目录: {model_dir}")
            logger.info(f"模型文件: {model_path}")
            logger.info(f"标签文件: {tags_path}")
            
            if not model_path.exists():
                logger.error(f"模型文件不存在: {model_path}")
                return False
            
            if not tags_path.exists():
                logger.error(f"标签文件不存在: {tags_path}")
                return False
            
            # 验证文件大小
            model_size = model_path.stat().st_size
            tags_size = tags_path.stat().st_size
            logger.info(f"模型文件大小: {model_size} bytes ({model_size/1024/1024:.2f} MB)")
            logger.info(f"标签文件大小: {tags_size} bytes ({tags_size/1024:.2f} KB)")
            
            if model_size < 10_000_000:
                logger.warning(f"模型文件过小，可能损坏: {model_size} bytes")
            
            # 加载 ONNX 模型
            logger.info("延迟导入 onnxruntime...")
            try:
                import onnxruntime as ort
                from onnxruntime import InferenceSession
                logger.info(f"onnxruntime 版本: {ort.__version__}")
                logger.info(f"可用的 providers: {ort.get_available_providers()}")
            except Exception as e:
                logger.error(f"onnxruntime 导入失败: {e}", exc_info=True)
                return False
            
            logger.info("初始化 ONNX InferenceSession...")
            # 使用 CPUExecutionProvider，如果可用则使用 CUDAExecutionProvider
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            logger.info(f"尝试使用 providers: {providers}")
            
            try:
                self.model = InferenceSession(str(model_path), providers=providers)
                logger.info(f"ONNX 模型加载成功")
                logger.info(f"使用的 provider: {self.model.get_providers()}")
                logger.info(f"输入数量: {len(self.model.get_inputs())}")
                logger.info(f"输出数量: {len(self.model.get_outputs())}")
                
                # 打印输入输出信息
                for i, inp in enumerate(self.model.get_inputs()):
                    logger.info(f"输入[{i}]: name={inp.name}, shape={inp.shape}, type={inp.type}")
                for i, out in enumerate(self.model.get_outputs()):
                    logger.info(f"输出[{i}]: name={out.name}, shape={out.shape}, type={out.type}")
            except Exception as e:
                logger.error(f"ONNX InferenceSession 初始化失败: {e}", exc_info=True)
                return False
            
            # 加载标签文件
            logger.info("加载标签文件...")
            self.tags = []
            self.general_index = None
            self.character_index = None
            
            try:
                with open(tags_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    header = next(reader)  # 跳过表头
                    logger.debug(f"CSV 表头: {header}")
                    
                    for row_num, row in enumerate(reader):
                        if len(row) < 3:
                            logger.warning(f"跳过无效行 {row_num}: {row}")
                            continue
                        
                        # 识别 general 和 character 标签的起始位置
                        if self.general_index is None and row[2] == "0":
                            self.general_index = row_num
                            logger.debug(f"找到 general_index: {row_num}")
                        elif self.character_index is None and row[2] == "4":
                            self.character_index = row_num
                            logger.debug(f"找到 character_index: {row_num}")
                        
                        # 保留原始标签名（不替换下划线）
                        self.tags.append(row[1])
                
                logger.info(f"标签加载完成: 总数 {len(self.tags)}")
            except Exception as e:
                logger.error(f"加载标签文件失败: {e}", exc_info=True)
                return False
            
            if self.general_index is None or self.character_index is None:
                logger.error(f"标签文件格式错误：无法找到 general/character 分类")
                logger.error(f"general_index={self.general_index}, character_index={self.character_index}")
                return False
            
            self._loaded = True
            logger.info(f"WD14 模型加载成功: {model_path}")
            logger.info(f"标签数: {len(self.tags)}, general_index: {self.general_index}, character_index: {self.character_index}")
            logger.info("=" * 60)
            return True
            
        except Exception as e:
            logger.error(f"加载 WD14 模型失败: {e}", exc_info=True)
            return False
    
    def preprocess_image(self, image: Image.Image) -> np.ndarray:
        """
        预处理图片 - 严格按照 pythongosssss 的实现
        
        1. 缩放到 INPUT_SIZE 范围内，保持宽高比
        2. 用白色填充成正方形
        3. RGB -> BGR 转换
        4. 转换为 float32 并添加 batch 维度
        """
        # 1. 计算缩放比例，缩小到最大尺寸
        ratio = float(INPUT_SIZE) / max(image.size)
        new_size = tuple(max(1, int(x * ratio)) for x in image.size)
        image = image.resize(new_size, Image.LANCZOS)
        
        # 2. 创建白色正方形画布
        square = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (255, 255, 255))
        # 将缩放后的图片粘贴到中心
        square.paste(image, ((INPUT_SIZE - new_size[0]) // 2, (INPUT_SIZE - new_size[1]) // 2))
        
        # 3. 转换为 numpy 数组
        image_array = np.array(square).astype(np.float32)
        
        # 4. RGB -> BGR 转换（关键！模型期望 BGR 输入）
        image_array = image_array[:, :, ::-1]
        
        # 5. 添加 batch 维度
        image_array = np.expand_dims(image_array, 0)
        
        return image_array
    
    def tag_image(
        self,
        image_path: str,
        general_threshold: float = DEFAULT_GENERAL_THRESHOLD,
        character_threshold: float = DEFAULT_CHARACTER_THRESHOLD,
        replace_underscore: bool = False
    ) -> List[Tuple[str, float]]:
        """
        对单张图片打标签
        
        Args:
            image_path: 图片路径
            general_threshold: 通用标签置信度阈值
            character_threshold: 人物标签置信度阈值
            replace_underscore: 是否将标签中的下划线替换为空格
            
        Returns:
            列表，每项为 (标签名, 置信度)
        """
        logger.debug(f"tag_image 开始: {image_path}")
        
        if not self.load_model():
            logger.error(f"模型未加载，无法打标签: {image_path}")
            return []
        
        try:
            # 读取图片
            logger.debug(f"读取图片: {image_path}")
            with Image.open(image_path) as opened:
                oriented = ImageOps.exif_transpose(opened).convert('RGBA')
                background = Image.new('RGBA', oriented.size, 'white')
                image = Image.alpha_composite(background, oriented).convert('RGB')
            logger.debug(f"图片尺寸: {image.size}, 模式: {image.mode}")
            
            # 预处理
            logger.debug("预处理图片...")
            image_array = self.preprocess_image(image)
            logger.debug(f"预处理后数组形状: {image_array.shape}, 类型: {image_array.dtype}")
            
            # 推理
            logger.debug("开始 ONNX 推理...")
            input_tensor = self.model.get_inputs()[0]
            label_name = self.model.get_outputs()[0].name
            logger.debug(f"输入名: {input_tensor.name}, 输出名: {label_name}")
            
            probs = self.model.run([label_name], {input_tensor.name: image_array})[0]
            logger.debug(f"推理完成，输出形状: {probs.shape}")
            
            # 组合标签和概率
            result = list(zip(self.tags, probs[0]))
            logger.debug(f"标签概率对数量: {len(result)}")
            
            # 按分类过滤标签
            logger.debug(f"过滤阈值: general={general_threshold}, character={character_threshold}")
            logger.debug(f"general_index={self.general_index}, character_index={self.character_index}")
            
            general = [item for item in result[self.general_index:self.character_index] 
                      if item[1] > general_threshold]
            character = [item for item in result[self.character_index:] 
                        if item[1] > character_threshold]
            
            logger.debug(f"过滤后: general={len(general)}, character={len(character)}")
            
            # 合并（character 在前，general 在后）
            all_tags = character + general
            
            # 可选：替换下划线为空格
            if replace_underscore:
                all_tags = [(tag.replace("_", " "), prob) for tag, prob in all_tags]
            
            # 按置信度降序排列
            all_tags.sort(key=lambda x: x[1], reverse=True)
            
            logger.debug(f"tag_image 完成: {image_path}, 标签数: {len(all_tags)}")
            if all_tags:
                logger.debug(f"Top 5 标签: {all_tags[:5]}")
            
            return all_tags
            
        except Exception as e:
            logger.error(f"打标签失败 {image_path}: {e}", exc_info=True)
            return []
    
    def tag_images_batch(
        self,
        image_paths: List[str],
        general_threshold: float = DEFAULT_GENERAL_THRESHOLD,
        character_threshold: float = DEFAULT_CHARACTER_THRESHOLD,
        replace_underscore: bool = False,
        progress_callback=None
    ) -> Dict[str, List[Tuple[str, float]]]:
        """
        批量打标签
        
        Args:
            image_paths: 图片路径列表
            general_threshold: 通用标签置信度阈值
            character_threshold: 人物标签置信度阈值
            replace_underscore: 是否将标签中的下划线替换为空格
            progress_callback: 进度回调函数，签名 callback(current, total)
            
        Returns:
            字典，key 为图片路径，value 为标签列表
        """
        results = {}
        total = len(image_paths)
        
        for i, image_path in enumerate(image_paths):
            tags = self.tag_image(image_path, general_threshold, character_threshold, replace_underscore)
            results[image_path] = tags
            
            if progress_callback:
                progress_callback(i + 1, total)
        
        return results


# 全局实例
_tagger_instance = None


def get_tagger() -> WD14Tagger:
    """获取全局 tagger 实例"""
    global _tagger_instance
    if _tagger_instance is None:
        _tagger_instance = WD14Tagger()
    return _tagger_instance
