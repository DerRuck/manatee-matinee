from google.cloud import discoveryengine_v1 as discoveryengine
from google.api_core.exceptions import GoogleAPICallError

from core.settings import get_settings


class SearchResult:
    def __init__(self, doc_id: str, title: str, snippet: str, raw: dict):
        self.doc_id = doc_id
        self.title = title
        self.snippet = snippet
        self.raw = raw

    def __repr__(self):
        return f"SearchResult(doc_id={self.doc_id!r}, title={self.title!r})"


class VertexSearchClient:
    def __init__(self, datastore_id: str | None = None):
        settings = get_settings()

        self.project_id = settings.gcp_project_id
        self.location = settings.gcp_location
        self.datastore_id = datastore_id or settings.firestore_leads_collection

        self.client = discoveryengine.SearchServiceClient()

        self.serving_config = (
            f"projects/{self.project_id}"
            f"/locations/{self.location}"
            f"/dataStores/{self.datastore_id}"
            f"/servingConfigs/default_config"
        )

    def query(
        self,
        query_text: str,
        max_results: int = 10,
        filter_str: str | None = None,
    ) -> list[SearchResult]:
        """
        Run a search query against the Vertex AI Search datastore.

        Args:
            query_text:  Natural language query string.
            max_results: Max number of results to return (up to 100).
            filter_str:  Optional CEL filter expression, e.g. "status='active'".

        Returns:
            List of SearchResult objects, ordered by relevance.
        """
        request = discoveryengine.SearchRequest(
            serving_config=self.serving_config,
            query=query_text,
            page_size=max_results,
            filter=filter_str or "",
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True,
                    max_snippet_count=1,
                )
            ),
        )

        try:
            response = self.client.search(request)
            return [self._parse_result(r) for r in response.results]
        except GoogleAPICallError as e:
            raise RuntimeError(f"Vertex AI Search query failed: {e}") from e

    def _parse_result(self, result) -> SearchResult:
        """Extract doc_id, title, and snippet from a raw Vertex Search result."""
        doc = result.document
        doc_id = doc.id

        struct_data = dict(doc.struct_data) if doc.struct_data else {}
        title = struct_data.get("title") or struct_data.get("name") or doc_id

        snippets = getattr(result, "snippets", [])
        snippet = snippets[0].snippet if snippets else struct_data.get("description", "")

        return SearchResult(
            doc_id=doc_id,
            title=title,
            snippet=snippet,
            raw=struct_data,
        )
