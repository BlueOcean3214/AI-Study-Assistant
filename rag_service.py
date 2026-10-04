from pathlib import Path


KNOWLEDGE_PATH = Path("knowledge")


def load_documents():
    documents = []

    for file in KNOWLEDGE_PATH.glob("*.txt"):
        content = file.read_text(
            encoding="utf-8"
        )

        documents.append(content)

    return documents


def retrieve_context(query):
    """
    根据关键词寻找相关知识
    """

    documents = load_documents()

    results = []

    keywords = query.split()

    for doc in documents:
        score = 0

        for word in keywords:
            if word in doc:
                score += 1

        if score > 0:
            results.append(doc)

    if results:
        return "\n".join(results)

    return ""