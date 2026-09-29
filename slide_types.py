from enum import Enum


class SlideClass(str, Enum):
    COVER = "cover"
    AGENDA = "agenda"
    SECTION = "section"
    CONTENT_TEXT = "content_text"
    CONTENT_VISUAL = "content_visual"
    COMPARISON = "comparison"
    PROCESS = "process"
    TIMELINE = "timeline"
    METRICS = "metrics"
    TABLE = "table"
    CHART = "chart"
    QUOTE = "quote"
    PROFILE = "profile"
    CLOSING = "closing"
    OTHER = "other"
