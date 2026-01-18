# RAG IT - Explainable Decision Intelligence System (EDIS)

## Chosen Problem Statement
In today's AI-driven world, systems make critical decisions daily—such as loan approvals, hiring screenings, healthcare treatments, fraud detection, and credit limits—but often operate as impenetrable black boxes. Customers face unexplained rejections, employees struggle to validate decisions, and regulators cannot audit for fairness or compliance. This leads to frustration, distrust, legal liabilities, fines, lawsuits, reputational damage, and lost revenue. Our project addresses this by building a Explainable Decision Intelligence System(EDIS) that transforms black-box AI into transparent, auditable, and fair decision-making, to demonstrate trust, efficiency, fairness, and compliance.

## Explanation of the Solution
### Key Features and Functionality
Our EDIS leverages Retrieval-Augmented Generation (RAG) to provide clear, policy-based explanations for approval decisions, whether for ongoing applications or historical audits. Inspired by document intelligence systems, it integrates bank policies as a configurable knowledge base to rationalize decisions, ensuring transparency and accountability.

### Key Features and Functionality
1. **Decision Input Layer**: Users upload PDFs or text files. The system displays the AI's decision (approve/reject) with a confidence score and supporting evidence from policy documents.

2. **RAG-Powered Explanation Engine**: The system retrieves relevant policy text from a vectorized knowledge store and generates concise explanations and criteria checks (e.g., "Meets minimum credit score: Yes"). If policies conflict, the system flags the case for human review.

3. **What-If Analysis**: A counterfactual simulator lets users tweak inputs (e.g., income) to see how decisions would change and provides actionable recommendations.

4. **Presentation Layer**: A lightweight UI (static HTML) visualizes decision rationale, matching criteria, and policy citations.

5. **Audit & Compliance**: All decisions are logged to the application database for traceability and audit review.

### How It Works (End-to-End Flow)
- **Data Ingestion**: Documents are stored via a pluggable storage adapter (local filesystem or cloud bucket) and processed into chunks.
- **Indexing & Embeddings**: Chunk embeddings are computed by a configurable embedding provider and stored in the database or a vector index.
- **Decision Processing**: The RAG engine retrieves top-matching chunks and a generative step (optional) produces human-readable explanations.
- **User Interaction**: Users interact via a web UI or API to view decisions, suggested metadata, and perform reviews.

This solution is provider-agnostic: storage, embedding, and model providers are pluggable via adapters so you can run locally or integrate any hosted provider.

## Tech Stack Used
- **Programming Languages**: Python.
- **Frameworks/Libraries**: FastAPI for API development; optional embedding libraries (e.g., `sentence-transformers`) for local embeddings.
- **APIs/Tools**: PostgreSQL for structured data; optional vector index (e.g., Qdrant) for similarity search.
- **Other Tools**: Docker for local development; Git for version control.

## Link to Demo Video
[Demo Video](https://example.com/demo-video)

## Link to Presentation Deck
[Presentation Deck](https://example.com/presentation-deck)

## Commit History
We maintain a clear commit history to showcase team contributions and project progress.
View the full history at [GitHub Commits](https://github.com/your-repo/commits). 