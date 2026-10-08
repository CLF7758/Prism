"""
DeepSeek API 客户端模块
用于 AI 标签后处理：翻译标签、生成标题、归纳分类
"""
import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple
from PyQt6.QtCore import QSettings

logger = logging.getLogger(__name__)

# 配置键名
SETTINGS_KEY_API_KEY = "AI/deepseek_api_key"
SETTINGS_KEY_BASE_URL = "AI/deepseek_base_url"
SETTINGS_KEY_MODEL = "AI/deepseek_model"
SETTINGS_KEY_PRIVACY_CONFIRMED = "AI/privacy_confirmed"
SETTINGS_KEY_LOCAL_ONLY = "AI/local_only_mode"
SETTINGS_KEY_TRANSLATE_ENABLED = "AI/translate_enabled"
SETTINGS_KEY_TITLE_ENABLED = "AI/title_enabled"
SETTINGS_KEY_CATEGORY_ENABLED = "AI/category_enabled"
SETTINGS_KEY_MONTHLY_COST = "AI/monthly_cost"

# 默认值
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 费用估算（DeepSeek 定价：百万 token 约 1-2 元）
COST_PER_MILLION_TOKENS = 1.5  # 元


class DeepSeekClient:
    """DeepSeek API 客户端"""
    
    def __init__(self):
        self.settings = QSettings()
        self._total_tokens_used = 0
    
    def get_api_key(self) -> str:
        """获取 API Key"""
        return self.settings.value(SETTINGS_KEY_API_KEY, "") or ""
    
    def get_base_url(self) -> str:
        """获取 API Base URL"""
        return self.settings.value(SETTINGS_KEY_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    
    def get_model(self) -> str:
        """获取模型名"""
        return self.settings.value(SETTINGS_KEY_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL
    
    def is_privacy_confirmed(self) -> bool:
        """用户是否已确认隐私协议"""
        return self.settings.value(SETTINGS_KEY_PRIVACY_CONFIRMED, False, type=bool)
    
    def set_privacy_confirmed(self, confirmed: bool):
        """设置隐私协议确认状态"""
        self.settings.setValue(SETTINGS_KEY_PRIVACY_CONFIRMED, confirmed)
    
    def is_local_only_mode(self) -> bool:
        """是否为纯本地模式（不调用 API）"""
        return self.settings.value(SETTINGS_KEY_LOCAL_ONLY, False, type=bool)
    
    def is_translate_enabled(self) -> bool:
        """是否启用标签翻译"""
        return self.settings.value(SETTINGS_KEY_TRANSLATE_ENABLED, True, type=bool)
    
    def is_title_enabled(self) -> bool:
        """是否启用标题生成"""
        return self.settings.value(SETTINGS_KEY_TITLE_ENABLED, True, type=bool)
    
    def is_category_enabled(self) -> bool:
        """是否启用分类归纳"""
        return self.settings.value(SETTINGS_KEY_CATEGORY_ENABLED, True, type=bool)
    
    def call_api(self, prompt: str, system_prompt: str = "") -> Tuple[Optional[str], int]:
        """
        调用 DeepSeek API
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示（可选）
            
        Returns:
            (响应文本, token 用量) 元组
        """
        api_key = self.get_api_key()
        if not api_key:
            logger.error("API Key 未设置")
            return None, 0
        
        base_url = self.get_base_url()
        model = self.get_model()
        
        # 构建请求
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1000
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        url = f"{base_url}/v1/chat/completions"
        
        try:
            # 发送请求
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                
                # 提取响应
                content = result['choices'][0]['message']['content']
                
                # 统计 token
                usage = result.get('usage', {})
                tokens = usage.get('total_tokens', 0)
                self._total_tokens_used += tokens
                
                return content, tokens
                
        except urllib.error.HTTPError as e:
            logger.error(f"API 请求失败: {e.code} {e.reason}")
            try:
                error_body = e.read().decode('utf-8')
                logger.error(f"错误详情: {error_body}")
            except:
                pass
            return None, 0
            
        except Exception as e:
            logger.error(f"API 请求异常: {e}")
            return None, 0
    
    def translate_tags(self, tags: List[str]) -> List[str]:
        """
        将英文标签翻译成中文
        
        Args:
            tags: 英文标签列表
            
        Returns:
            中文标签列表
        """
        if not tags:
            return []
        
        prompt = f"将以下英文标签翻译成中文，每行一个标签，保持简洁，不要添加编号或其他格式：\n" + "\n".join(tags)
        
        response, tokens = self.call_api(
            prompt,
            system_prompt="你是一个专业的图片标签翻译助手，将英文标签翻译成简洁的中文。只输出翻译结果，每行一个标签，不要任何编号、解释或额外文字。"
        )
        
        if not response:
            return tags  # 失败时返回原文
        
        # 解析结果，去除可能的编号和空白
        import re
        translated = []
        for line in response.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            # 去除可能的编号前缀（如 "1. ", "1) ", "- ", "* "）
            line = re.sub(r'^[\d]+[.)]\s*', '', line)
            line = re.sub(r'^[-*•]\s*', '', line)
            if line:
                translated.append(line)
        
        # 确保数量一致
        if len(translated) != len(tags):
            logger.warning(f"翻译结果数量不匹配: 期望 {len(tags)}, 实际 {len(translated)}")
            return tags
        
        return translated
    
    def generate_title(self, tags: List[str]) -> str:
        """
        根据标签生成自然语言标题
        
        Args:
            tags: 标签列表
            
        Returns:
            标题文本
        """
        if not tags:
            return ""
        
        # 取前 10 个标签
        top_tags = tags[:10]
        
        prompt = f"根据以下图片标签，生成一句简洁的中文标题（10-20字）：\n{', '.join(top_tags)}"
        
        response, tokens = self.call_api(
            prompt,
            system_prompt="你是一个图片描述专家，根据标签生成简洁的中文标题。只输出标题，不要任何解释。"
        )
        
        if not response:
            return ""
        
        return response.strip()
    
    def suggest_categories(self, tags: List[str], existing_categories: List[str]) -> Dict[str, str]:
        """
        根据标签建议分类
        
        Args:
            tags: 标签列表
            existing_categories: 现有分类列表
            
        Returns:
            字典：{"建议分类": "已有分类名" 或 "__NEW__:新分类名"}
        """
        if not tags or not existing_categories:
            return {}
        
        prompt = f"""现有分类：{', '.join(existing_categories)}

图片标签：{', '.join(tags[:15])}

请判断这些标签最适合归入哪个现有分类。如果都不合适，建议一个新分类名。
输出格式：
- 如果适合现有分类：直接输出分类名
- 如果需要新分类：输出 __NEW__:新分类名"""
        
        response, tokens = self.call_api(
            prompt,
            system_prompt="你是一个图片分类助手，根据标签内容建议合适的分类。只输出分类名，不要任何解释。"
        )
        
        if not response:
            return {}
        
        category = response.strip()
        
        result = {}
        if category.startswith("__NEW__:"):
            result["suggested"] = category
        elif category in existing_categories:
            result["suggested"] = category
        else:
            result["suggested"] = category  # 当作新分类
        
        return result
    
    def get_total_cost(self) -> float:
        """获取累计费用（元）"""
        return self._total_tokens_used * COST_PER_MILLION_TOKENS / 1_000_000
    
    def get_monthly_cost(self) -> float:
        """获取本月累计费用（元）"""
        cost_str = self.settings.value(SETTINGS_KEY_MONTHLY_COST, "0") or "0"
        try:
            return float(cost_str)
        except:
            return 0.0
    
    def add_monthly_cost(self, tokens: int):
        """累加本月费用"""
        current = self.get_monthly_cost()
        added = tokens * COST_PER_MILLION_TOKENS / 1_000_000
        self.settings.setValue(SETTINGS_KEY_MONTHLY_COST, str(current + added))


# 全局实例
_client_instance = None


def get_client() -> DeepSeekClient:
    """获取全局 DeepSeek 客户端实例"""
    global _client_instance
    if _client_instance is None:
        _client_instance = DeepSeekClient()
    return _client_instance
