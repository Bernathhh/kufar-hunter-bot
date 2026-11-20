"""
Telegram Bot API client implementation using standard library.
Supports text, photos, inline buttons, reply keyboards, and callback queries.
"""
import urllib.request
import urllib.parse
import json
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("telegram_api")


class TelegramClient:
    def __init__(self, token: str):
        self.token = token.strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def _api_call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 35) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/{method}"
        data = None
        headers = {}
        
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_bytes = response.read()
                res_json = json.loads(res_bytes.decode("utf-8"))
                if res_json.get("ok"):
                    return res_json.get("result")
                else:
                    logger.error(f"Telegram API error ({method}): {res_json.get('description')}")
                    return None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.error(f"HTTPError on {method}: {e.code} - {err_body}")
            return None
        except Exception as e:
            logger.error(f"Exception on {method}: {e}")
            return None

    def get_me(self) -> Optional[Dict[str, Any]]:
        """Verifies bot token and returns bot info."""
        return self._api_call("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> List[Dict[str, Any]]:
        """Long polls for incoming updates."""
        payload: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        res = self._api_call("getUpdates", payload=payload, timeout=timeout + 10)
        return res if isinstance(res, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Sends a text message with optional reply_markup."""
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._api_call("sendMessage", payload=payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Edits an existing text message."""
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = self._api_call("editMessageText", payload=payload)
        return res is not None

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False
    ) -> bool:
        """Acknowledges a callback query from inline buttons."""
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        res = self._api_call("answerCallbackQuery", payload=payload)
        return res is not None

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Deletes a message."""
        payload = {"chat_id": chat_id, "message_id": message_id}
        res = self._api_call("deleteMessage", payload=payload)
        return res is not None

    def send_photo(
        self,
        chat_id: int,
        photo_url: str,
        caption: Optional[str] = None,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Sends a photo with optional caption and reply_markup.
        Falls back to send_message if sending photo directly fails.
        """
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "parse_mode": parse_mode
        }
        if caption:
            payload["caption"] = caption
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        res = self._api_call("sendPhoto", payload=payload)
        if res is not None:
            return True

        # Fallback to sendMessage if photo URL is rejected by Telegram
        if caption:
            logger.info("Photo sending failed, falling back to sendMessage")
            return self.send_message(chat_id, caption, parse_mode=parse_mode, reply_markup=reply_markup) is not None
        return False
