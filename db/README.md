Enable `pgvector` extension

Run one of the following (authenticated to the project) to enable pgvector on the Cloud SQL Postgres instance:

gcloud sql connect sop-metadata-db --project=ctrlaltelite-484111 --user=sop_user --quiet < create_pgvector_extension.sql

Or, using psql with Cloud SQL Auth Proxy:

# start cloud sql auth proxy locally and then:
psql "host=127.0.0.1 port=5432 dbname=sop_database user=sop_user" -f create_pgvector_extension.sql

Migration steps to use pgvector natively:

1. Enable the extension:

	gcloud sql connect sop-metadata-db --project=ctrlaltelite-484111 --user=sop_user --quiet < create_pgvector_extension.sql

2. Apply schema migration to add `embedding_vector` and remove old `embedding` column:

	gcloud sql connect sop-metadata-db --project=ctrlaltelite-484111 --user=sop_user --quiet < migrate_add_embedding_vector.sql

3. If you have existing data serialized in `embedding`, run the backfill script locally (via Cloud SQL Auth Proxy) or on a secure VM:

	# set DB_PASSWORD or use secret manager
	export DB_HOST=127.0.0.1
	export DB_USER=sop_user
	export DB_NAME=sop_database
	export DB_PASSWORD=...
	python scripts/backfill_embeddings.py

4. Run `ANALYZE documents;` and optionally adjust ivfflat index parameters.

