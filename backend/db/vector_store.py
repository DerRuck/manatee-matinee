import os
from langchain_core.documents import Document
from langchain_google_vertexai import VertexAIEmbeddings

embeddings = VertexAIEmbeddings(model_name="text-embedding-004")

def save_document_to_db(drive_file_id: str, extracted_data: dict):
    # Construct the LangChain document
    searchable_text = f"Name: {extracted_data.get('name')}. Job Title: {extracted_data.get('job_title')}. Company: {extracted_data.get('organization')}."
    doc = Document(page_content=searchable_text, metadata={"drive_file_id": drive_file_id, **extracted_data})
    
    ENV = os.getenv("ENV", "local")

    if ENV == "local":
        from langchain_chroma import Chroma
        vector_store = Chroma(
            collection_name="documents",
            embedding_function=embeddings,
            persist_directory="./chroma_db"
        )
        vector_store.add_documents([doc])
        print("Saved to ChromaDB")
    else:
        from langchain_google_firestore import FirestoreVectorStore
        vector_store = FirestoreVectorStore(
            collection="documents",
            embedding_service=embeddings,
        )
        vector_store.add_documents([doc])
        print("Saved to Firestore")