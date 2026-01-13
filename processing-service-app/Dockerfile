# Use the official lightweight Python image.
FROM python:3.11-slim-bookworm

# Install system dependencies required for google-cloud-sql-connector and pg8000
RUN apt-get update && apt-get install -y \
	build-essential \
	libpq-dev \
	&& rm -rf /var/lib/apt/lists/*

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED True

# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install production dependencies.
ENV PIP_FLAGS="--no-cache-dir --upgrade"

# First, update pip to the latest version
RUN pip install --upgrade pip

# Then, install application dependencies
RUN pip install ${PIP_FLAGS} -r requirements.txt

# Run the web service on container startup.
CMD exec functions-framework --target=process_sop_document --port=8080 --signature-type=cloudevent
