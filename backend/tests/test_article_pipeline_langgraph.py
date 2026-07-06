"""LangGraph orchestration foundation tests."""

from app.orchestration.article_pipeline import article_load_graph


def test_article_load_graph_compiles() -> None:
    graph = article_load_graph()

    assert graph.__class__.__name__ == "CompiledStateGraph"
