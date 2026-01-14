# RAG IT - Explainable Decision Intelligence System (EDIS)

## Chosen Problem Statement
In today's AI-driven world, systems make critical decisions daily—such as loan approvals, hiring screenings, healthcare treatments, fraud detection, and credit limits—but often operate as impenetrable black boxes. Customers face unexplained rejections, employees struggle to validate decisions, and regulators cannot audit for fairness or compliance. This leads to frustration, distrust, legal liabilities, fines, lawsuits, reputational damage, and lost revenue. Our project addresses this by building a Explainable Decision Intelligence System(EDIS) that transforms black-box AI into transparent, auditable, and fair decision-making, to demonstrate trust, efficiency, fairness, and compliance.

## Explanation of the Solution
Our EDIS leverages Retrieval-Augmented Generation (RAG) to provide clear, policy-based explanations for approval decisions, whether for ongoing applications or historical audits. Inspired by document intelligence systems, it integrates bank policies as a knowledge base to rationalize decisions, ensuring transparency and accountability.

### Key Features and Functionality
1. **Decision Input Layer**: Users input data via PDF, txt. The system displays the AI's decision (approve/reject) with a confidence score. For example, it shows "Loan Rejected" with 85% confidence, pulling from historical data.

2. **RAG-Powered Explanation Engine**: Using Google Vertex AI and a vectorized policy database, the system retrieves relevant bank policies (e.g., credit score thresholds, income requirements) to explain decisions. It "ticks" criteria boxes—e.g., "Meets minimum credit score: Yes" or "Debt-to-income ratio exceeds limit: No"—and generates natural language rationales. If policies conflict, it flags for human review, preventing errors.

3. **What-If Analysis**: A counterfactual simulator allows users to tweak variables (e.g., "Change income to $60,000") and see how the decision changes. It provides actionable recommendations, like "Increase credit score by 50 points to qualify," helping customers improve applications and reducing false positives.

4. **Presentation Layer**: A clean UI built with HTML offers data visualizations (charts for criteria matching). Visuals include decision trees and policy citations.

5. **Trust & Validation Layer**: Every decision includes confidence scores, full audit trails (logged in Cloud Logging). All interactions are traceable, ensuring compliance and building trust.

### How It Works (End-to-End Flow)
- **Data Ingestion**: Data and policies are ingested into Google Cloud Storage (GCS) and indexed via Vertex AI for RAG.
- **Decision Processing**: Vertex AI models make initial decisions; our engine retrieves policies to explain them.
- **User Interaction**: Via a web UI or API, users explore explanations and what-if scenarios.
- **Audit & Compliance**: All logs are stored for regulatory review, automating compliance and reducing manual audits.

This solution saves banks time and money by automating explanations, cutting false positives through better accuracy, and fostering trust. It scales with Google Cloud's infrastructure and is practical for real-world deployment, enabling innovation in AI applications while prioritizing fairness. By making AI decisions understandable, it empowers customers, validates employee actions, and satisfies regulators—turning black boxes into trusted allies.

## Tech Stack Used
- **Programming Languages**: Python (for backend logic and AI integrations).
- **Frameworks/Libraries**: 
  - FastAPI (for API development, based on existing services).
  - Google Vertex AI (for RAG, embeddings, and generative models).
  - HTML (for UI/UX and form inputs).
- **APIs/Tools**:
  - Google Cloud Storage (GCS) (for document/policy storage).
  - PostgreSQL (for data storage).
  - Vertex AI RAG Corpus (for model deployment and RAG pipelines).
  - Gemini (for text generation and explanations).
  - Cloud Logging (for audit trails).
- **Other Tools**:
  - Terraform (for infrastructure, from existing configs).
  - Docker (for containerization).
  - Git (for version control and commit history).

## Link to Demo Video
[Demo Video](https://example.com/demo-video)

## Link to Presentation Deck
[Presentation Deck](https://example.com/presentation-deck)

## Commit History
We maintain a clear commit history to showcase team contributions and project progress.
View the full history at [GitHub Commits](https://github.com/your-repo/commits). 