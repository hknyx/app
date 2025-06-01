FROM public.ecr.aws/lambda/python:3.8

# Install system dependencies
RUN yum update -y && \
    yum install -y \
    gcc \
    gcc-c++ \
    python3-devel \
    make \
    && yum clean all

# Copy requirements first to leverage Docker cache
COPY requirements.txt ${LAMBDA_TASK_ROOT}

# Add user
USER nobody

# Add healthcheck
HEALTHCHECK CMD curl --fail http://localhost:8080/health || exit 1


# Install Python packages with explicit version control
RUN pip install --upgrade pip && \
    pip install --no-cache-dir "numpy==1.19.5" && \
    pip install --no-cache-dir "scipy==1.6.3" && \
    pip install --no-cache-dir "faiss-cpu==1.7.1" && \
    pip install --no-cache-dir "langchain>=0.1.0" && \
    pip install --no-cache-dir "langchain-community>=0.0.10" && \
    pip install --no-cache-dir -U boto3 botocore

# Verify installations
RUN pip list && python -c "import faiss; import langchain; import langchain_community; print(f'Faiss version: {faiss.__version__}')"

# Copy function code
COPY index.py ${LAMBDA_TASK_ROOT}
COPY tools.py ${LAMBDA_TASK_ROOT}
COPY local_index ${LAMBDA_TASK_ROOT}/local_index

# Set the CMD to your handler
CMD [ "index.handler" ]
