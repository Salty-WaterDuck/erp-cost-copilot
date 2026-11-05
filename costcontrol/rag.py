"""本地 RAG：在零依赖前提下，对项目文档做检索增强。

不接向量库、不调 embedding：用 BM25 + 中文 bigram 做关键词检索。
适合「成本口径怎么定」「未分配费用怎么处理」「某个配置项在哪」这类
术语密集、答案藏在文档里的问题。

输出只给「原文段落 + 来源」，不生成新事实；需要解释时再把命中段落交给 LLM。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATHS = (
    "README.md",
    "docs",
    "config_templates",
    "data/erp_allocation_config.txt",
)

CJK = re.compile(r"[\u4e00-\u9fff]+")
ASCII = re.compile(r"[A-Za-z0-9_]+")
HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
STOP_CHARS = set("的了和是在与及对为等中按从到会要也就都而被把让一个我们")


@dataclass(frozen=True)
class Document:
    doc_id: str
    source: str
    heading: str
    text: str


@dataclass(frozen=True)
class Hit:
    document: Document
    score: float

    @property
    def snippet(self) -> str:
        text = re.sub(r"\s+", " ", self.document.text).strip()
        return text[:260] + ("…" if len(text) > 260 else "")


def tokenize(text: str) -> list[str]:
    """中文按字 + bigram，英文按词；不引入分词依赖。"""
    lowered = text.lower()
    tokens: list[str] = []
    for run in CJK.findall(lowered):
        chars = [char for char in run if char not in STOP_CHARS]
        tokens.extend(chars)
        tokens.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
    tokens.extend(ASCII.findall(lowered))
    return tokens


def _split_long(text: str, limit: int = 1200) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buffer: list[str] = []
    size = 0
    for paragraph in text.split("\n\n"):
        if size + len(paragraph) > limit and buffer:
            parts.append("\n\n".join(buffer))
            buffer = []
            size = 0
        buffer.append(paragraph)
        size += len(paragraph)
    if buffer:
        parts.append("\n\n".join(buffer))
    return parts


def _chunk_markdown(text: str, source: str) -> list[Document]:
    documents: list[Document] = []
    heading = source
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        if len(body) < 40:
            return
        for index, part in enumerate(_split_long(body), 1):
            suffix = f"#{index}" if index > 1 else ""
            documents.append(
                Document(
                    doc_id=f"{source}::{heading}{suffix}",
                    source=source,
                    heading=heading,
                    text=part,
                )
            )

    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            buffer = [line]
            heading = match.group(2).strip()
        else:
            buffer.append(line)
    flush()
    return documents


def _chunk_yaml(text: str, source: str) -> list[Document]:
    blocks = re.split(r"(?m)^(?=[A-Za-z_][\w-]*:)", text)
    documents = []
    for index, block in enumerate(blocks, 1):
        block = block.strip()
        if len(block) < 40:
            continue
        first = block.splitlines()[0].rstrip(":")
        documents.append(
            Document(
                doc_id=f"{source}::{first}#{index}",
                source=source,
                heading=first,
                text=block,
            )
        )
    return documents


def build_documents(root: str | Path | None = None) -> list[Document]:
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    documents: list[Document] = []
    for relative in DEFAULT_PATHS:
        target = base / relative
        if target.is_file():
            documents.extend(_load_file(target, base))
        elif target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".md", ".yaml", ".yml", ".txt"}:
                    documents.extend(_load_file(path, base))
    return documents


def _load_file(path: Path, base: Path) -> list[Document]:
    text = path.read_text(encoding="utf-8")
    source = path.relative_to(base).as_posix()
    if path.suffix.lower() in {".md"}:
        return _chunk_markdown(text, source)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return _chunk_yaml(text, source)
    return [Document(doc_id=source, source=source, heading=source, text=text)]


class BM25Index:
    def __init__(self, documents: list[Document]) -> None:
        if not documents:
            raise ValueError("没有可索引的文档")
        self.documents = documents
        self.tokens = [tokenize(document.text) for document in documents]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg_length = sum(self.lengths) / len(self.lengths)
        self.frequencies = [Counter(tokens) for tokens in self.tokens]
        self.document_frequency: Counter[str] = Counter()
        for tokens in self.tokens:
            self.document_frequency.update(set(tokens))
        self.total = len(documents)

    def _idf(self, term: str) -> float:
        df = self.document_frequency.get(term, 0)
        return math.log(1 + (self.total - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 3) -> list[Hit]:
        terms = tokenize(query)
        if not terms:
            raise ValueError("查询词为空或无法分词")
        scores: list[tuple[float, int]] = []
        for index, frequency in enumerate(self.frequencies):
            length = self.lengths[index] or 1
            score = 0.0
            for term in terms:
                tf = frequency.get(term, 0)
                if not tf:
                    continue
                denominator = tf + 1.5 * (1 - 0.75 + 0.75 * length / self.avg_length)
                score += self._idf(term) * tf * 2.5 / denominator
            if score > 0:
                scores.append((score, index))
        scores.sort(reverse=True)
        return [
            Hit(document=self.documents[index], score=round(score, 4))
            for score, index in scores[:k]
        ]


_INDEX: BM25Index | None = None


def get_index(root: str | Path | None = None) -> BM25Index:
    global _INDEX
    if _INDEX is None:
        _INDEX = BM25Index(build_documents(root))
    return _INDEX


def search(query: str, k: int = 3, root: str | Path | None = None) -> list[Hit]:
    return get_index(root).search(query, k=k)


def render_hits(query: str, hits: list[Hit]) -> str:
    lines = [f"## RAG 检索 · {query}", ""]
    if not hits:
        lines.append("没有命中。换一个更具体的术语，例如「未分配制造费用」「退料」「约当产量」。")
        return "\n".join(lines)
    for index, hit in enumerate(hits, 1):
        lines.append(
            f"{index}. **{hit.document.source}** · "
            f"{hit.document.heading}（score {hit.score}）"
        )
        lines.append(f"   {hit.snippet}")
        lines.append("")
    lines.append("> 只返回原文段落与来源，不生成新事实；接入模型时把命中段落作为上下文。")
    return "\n".join(lines)


__all__ = [
    "BM25Index",
    "Document",
    "Hit",
    "build_documents",
    "get_index",
    "render_hits",
    "search",
    "tokenize",
]
