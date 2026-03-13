from google.cloud import discoveryengine

class VertexSearchClient:

    def __init__(self, project_id, location, datastore_id):
        self.client = discoveryengine.SearchServiceClient()