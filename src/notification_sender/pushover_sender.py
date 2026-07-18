# -*- coding: utf-8 -*-
"""
Pushover 发送提醒服务

职责：
1. 通过 Pushover API 发送 Pushover 消息
"""
import logging
import math
from typing import Optional
from datetime import datetime
import requests

from src.config import Config
from src.formatters import markdown_to_plain_text


logger = logging.getLogger(__name__)


PUSHOVER_MAX_MESSAGE_LENGTH = 1024


class PushoverSender:
    
    def __init__(self, config: Config):
        """
        初始化 Pushover 配置

        Args:
            config: 配置对象
        """
        self._pushover_config = {
            'user_key': getattr(config, 'pushover_user_key', None),
            'api_token': getattr(config, 'pushover_api_token', None),
        }
        
    def _is_pushover_configured(self) -> bool:
        """检查 Pushover 配置是否完整"""
        return bool(self._pushover_config['user_key'] and self._pushover_config['api_token'])

    def send_to_pushover(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Pushover
        
        Pushover API 格式：
        POST https://api.pushover.net/1/messages.json
        {
            "token": "应用 API Token",
            "user": "用户 Key",
            "message": "消息内容",
            "title": "标题（可选）"
        }
        
        Pushover 特点：
        - 支持 iOS/Android/桌面多平台推送
        - 消息限制 1024 字符
        - 支持优先级设置
        - 支持 HTML 格式
        
        Args:
            content: 消息内容（Markdown 格式，会转为纯文本）
            title: 消息标题（可选，默认为"股票分析报告"）

        Returns:
            是否发送成功
        """
        if not self._is_pushover_configured():
            logger.warning("Pushover 配置不完整，跳过推送")
            return False
        
        user_key = self._pushover_config['user_key']
        api_token = self._pushover_config['api_token']
        
        # Pushover API 端点
        api_url = "https://api.pushover.net/1/messages.json"
        
        # 处理消息标题
        if title is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
            title = f"📈 股票分析报告 - {date_str}"
        
        # 转换 Markdown 为纯文本（Pushover 支持 HTML，但纯文本更通用）
        plain_content = markdown_to_plain_text(content)
        
        if len(plain_content) <= PUSHOVER_MAX_MESSAGE_LENGTH:
            # 单条消息发送
            return self._send_pushover_message(api_url, user_key, api_token, plain_content, title, timeout_seconds=timeout_seconds)
        else:
            # 分段发送长消息
            return self._send_pushover_chunked(
                api_url,
                user_key,
                api_token,
                plain_content,
                title,
                PUSHOVER_MAX_MESSAGE_LENGTH,
                timeout_seconds=timeout_seconds,
            )
      
    def _send_pushover_message(
        self, 
        api_url: str, 
        user_key: str, 
        api_token: str, 
        message: str, 
        title: str,
        priority: int = 0,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        发送单条 Pushover 消息
        
        Args:
            api_url: Pushover API 端点
            user_key: 用户 Key
            api_token: 应用 API Token
            message: 消息内容
            title: 消息标题
            priority: 优先级 (-2 ~ 2，默认 0)
        """
        try:
            payload = {
                "token": api_token,
                "user": user_key,
                "message": message,
                "title": title,
                "priority": priority,
            }
            
            response = requests.post(api_url, data=payload, timeout=timeout_seconds or 30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('status') == 1:
                    logger.info("Pushover 消息发送成功")
                    return True
                else:
                    errors = result.get('errors', ['未知错误'])
                    logger.error(f"Pushover 返回错误: {errors}")
                    return False
            else:
                logger.error(f"Pushover 请求失败: HTTP {response.status_code}")
                logger.debug(f"响应内容: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"发送 Pushover 消息失败: {e}")
            return False
    
    def _send_pushover_chunked(
        self, 
        api_url: str, 
        user_key: str, 
        api_token: str, 
        content: str, 
        title: str,
        max_length: int,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        分段发送长 Pushover 消息
        
        按段落分割，确保每段不超过最大长度
        """
        import time
        
        chunks = self._split_pushover_content(content, max_length)
        
        total_chunks = len(chunks)
        success_count = 0
        
        logger.info(f"Pushover 分批发送：共 {total_chunks} 批")
        
        for i, chunk in enumerate(chunks):
            # 添加分页标记到标题
            chunk_title = f"{title} ({i+1}/{total_chunks})" if total_chunks > 1 else title
            
            if self._send_pushover_message(
                api_url,
                user_key,
                api_token,
                chunk,
                chunk_title,
                timeout_seconds=timeout_seconds,
            ):
                success_count += 1
                logger.info(f"Pushover 第 {i+1}/{total_chunks} 批发送成功")
            else:
                logger.error(f"Pushover 第 {i+1}/{total_chunks} 批发送失败")
            
            # 批次间隔，避免触发频率限制
            if i < total_chunks - 1:
                time.sleep(1)

        return success_count == total_chunks

    @staticmethod
    def _split_pushover_content(content: str, max_length: int) -> list[str]:
        """Split plain text without leaving an oversized section or tiny tail."""

        if len(content) <= max_length:
            return [content]

        chunks = []
        remaining = content
        while len(remaining) > max_length:
            chunk_count = math.ceil(len(remaining) / max_length)
            target = math.ceil(len(remaining) / chunk_count)
            split_at = PushoverSender._find_split_point(remaining, target)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        chunks.append(remaining)
        return chunks

    @staticmethod
    def _find_split_point(content: str, target: int) -> int:
        """Prefer a nearby paragraph or line boundary, falling back to a hard cut."""

        minimum_preferred = max(1, target // 2)
        for separator in ("\n\n", "\n", " "):
            split_at = content.rfind(separator, 0, target + 1)
            if split_at >= minimum_preferred:
                return split_at + len(separator)
        return target
