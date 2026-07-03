from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.services.llm_utils import loads_json_array
from app.services.settings_store import get_llm_config


class LlmClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        load_saved: bool = True,
    ) -> None:
        saved = get_llm_config() if load_saved and not (base_url and api_key and model) else {}
        self.base_url = (base_url or saved.get("base_url") or settings.llm_base_url or "").rstrip("/")
        self.api_key = api_key or saved.get("api_key") or settings.llm_api_key
        self.model = model or saved.get("model") or settings.llm_model
        self.timeout = timeout or settings.llm_timeout
        self.last_error: str | None = None
        self.last_content: str | None = None
        self.last_parsed_count: int = 0
        self.last_image_size: tuple[int, int] | None = None
        self.last_image_bytes: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def summarize_portfolio(self, context: dict[str, Any]) -> str | None:
        if not self.configured:
            return None
        prompt = (
            "你是一个谨慎的基金组合研究助手。基于输入的持仓评分、涨跌幅和权重，"
            "输出不超过 5 条中文要点。只能给观察、风险提示、再平衡方向，"
            "不要给收益承诺，也不要给硬性买卖指令。"
        )
        content = self._chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return content.strip() if content else None

    def summarize_market_report(self, context: dict[str, Any]) -> str | None:
        if not self.configured:
            self.last_error = "大模型配置不完整"
            return None
        prompt = (
            "你是一个谨慎的A股基金研究助手。请基于输入的收盘日报数据，输出中文AI建议。"
            "要求：1）只做观察、风险提示、等待确认、回调观察、仓位纪律等研究建议；"
            "2）不得承诺收益，不得给出硬性买入/卖出指令；"
            "3）重点结合当日行业板块涨幅榜、跌幅榜、权益基金/ETF表现、市场指标和热点新闻；"
            "4）输出3到5条要点，每条不超过80字；"
            "5）如果数据矛盾或信号不充分，要明确提示不确定性。"
        )
        content = self._chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return content.strip() if content else None

    def parse_holdings_image(self, image: bytes, mime_type: str) -> list[dict[str, Any]]:
        return self.parse_holdings_images([(image, mime_type)])

    def parse_holdings_images(self, images: list[tuple[bytes, str]]) -> list[dict[str, Any]]:
        self.last_error = None
        self.last_content = None
        self.last_parsed_count = 0
        self.last_image_size = None
        self.last_image_bytes = None
        if not self.configured:
            self.last_error = "大模型配置不完整"
            return []

        all_items: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        errors: list[str] = []
        total_bytes = 0
        for index, (image, mime_type) in enumerate(images, start=1):
            compact_image, compact_mime = compact_image_for_llm(image, mime_type)
            total_bytes += len(compact_image)
            data_url = f"data:{compact_mime};base64,{base64.b64encode(compact_image).decode('ascii')}"
            prompt = (
                "逐字识别这张基金持仓截图。只返回 JSON 数组，不要解释，不要 Markdown。"
                "每个基金一条记录，字段固定为 code, name, shares, cost_amount, return_rate, raw_text, confidence。"
                "code 为 6 位基金代码；name 为完整基金名称。"
                "shares 只能填写截图中明确标注的持有份额、持仓份额、可用份额、基金份额、数量或持有数量。"
                "cost_amount 填截图中的持有金额、持仓金额、持仓市值、参考市值、当前市值或本金。"
                "return_rate 填截图中的持有收益率、持仓收益率或累计收益率，保留正负号。"
                "raw_text 必须抄录该基金在截图中的原始文字和数字，便于人工核对。"
                "confidence 是 0 到 1 的识别置信度。不要把收益率、持仓收益、昨日收益或涨跌幅填到 shares。"
                "看不到份额时 shares 填 null，不要猜测。"
                "示例：[{\"code\":\"000001\",\"name\":\"某基金A\",\"shares\":123.45,"
                "\"cost_amount\":1000.0,\"raw_text\":\"某基金A 000001 持有份额123.45 持有金额1000.00\",\"confidence\":0.95}]"
            )
            self.last_error = None
            content = self._chat(
                [
                    {"role": "system", "content": "你是严格的金融持仓截图 OCR 解析器，只输出 JSON。"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    },
                ],
                temperature=0,
            )
            if not content:
                errors.append(f"第 {index} 张图片：{self.last_error or '模型没有返回内容'}")
                continue
            raw_responses.append(f"第 {index} 张图片\n{content}")
            parsed = loads_json_array(content)
            items = [item for item in parsed if isinstance(item, dict)]
            if not items:
                preview = content.replace("\n", " ").strip()[:160]
                errors.append(f"第 {index} 张图片返回的不是 JSON 数组：{preview}")
                continue
            all_items.extend(items)

        self.last_image_bytes = total_bytes
        self.last_content = "\n\n".join(raw_responses) or None
        self.last_parsed_count = len(all_items)
        self.last_error = "；".join(errors) if errors else None
        return all_items

    def _chat(self, messages: list[dict[str, Any]], temperature: float) -> str | None:
        payload = json.dumps(
            {"model": self.model, "messages": messages, "temperature": temperature},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except BrokenPipeError:
            self.last_error = "模型接口在接收图片时断开连接，通常是图片过大、网关限制或模型不支持图片输入"
            return None
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")[:240]
            except Exception:
                body = ""
            self.last_error = f"模型接口 HTTP {exc.code}: {body or exc.reason}"
            return None
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "Broken pipe" in reason:
                self.last_error = "模型接口在接收图片时断开连接，通常是图片过大、网关限制或模型不支持图片输入"
            else:
                self.last_error = f"模型接口连接失败: {reason}"
            return None
        except TimeoutError:
            self.last_error = "模型接口请求超时"
            return None
        except json.JSONDecodeError:
            self.last_error = "模型接口返回的不是 JSON"
            return None

        choices = data.get("choices") or []
        if not choices:
            self.last_error = "模型接口没有返回 choices"
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            self.last_error = "模型返回的 message.content 不是字符串"
            return None
        return content


def compact_image_for_llm(image: bytes, mime_type: str) -> tuple[bytes, str]:
    try:
        from PIL import Image
    except Exception:
        return image, mime_type or "image/png"

    try:
        with Image.open(io.BytesIO(image)) as source:
            source = source.convert("RGB")
            max_side = 2000
            width, height = source.size
            scale = min(1.0, max_side / max(width, height))
            if scale < 1:
                source = source.resize((int(width * scale), int(height * scale)))
            output = io.BytesIO()
            source.save(output, format="JPEG", quality=86, optimize=True)
            compact = output.getvalue()
            return compact, "image/jpeg"
    except Exception:
        return image, mime_type or "image/png"
