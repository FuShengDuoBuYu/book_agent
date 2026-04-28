from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.agent.prompts import BOOK_AGENT_SYSTEM_PROMPT
from app.core.llm import get_chat_model


class BookAgent:
    def __init__(self) -> None:
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", BOOK_AGENT_SYSTEM_PROMPT),
                (
                    "human",
                    "用户手机号：{phone_num}\n用户问题：{message}",
                ),
            ]
        )
        self.chain = prompt | get_chat_model() | StrOutputParser()

    async def chat(self, phone_num: str, message: str) -> str:
        try:
            reply = await self.chain.ainvoke(
                {
                    "phone_num": phone_num,
                    "message": message,
                }
            )
        except Exception as exc:
            error_text = str(exc)
            if "Connection refused" in error_text or "Failed to connect" in error_text:
                raise ConnectionError(
                    "无法连接 Ollama。请确认已运行：ollama run qwen3:14b"
                ) from exc

            raise RuntimeError(f"LangChain Agent 调用失败：{error_text}") from exc

        return reply.strip()
