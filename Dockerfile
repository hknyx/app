FROM amazon/aws-lambda-python:3.13

# Install system dependencies
RUN dnf update -y && dnf install -y \
    mesa-libGL \
    libX11 \
    zip \
    unzip \
    tar \
    bzip2 \
    graphviz \
    && dnf clean all

# Add user
USER nobody

# Add healthcheck
HEALTHCHECK CMD curl --fail http://localhost:8080/health || exit 1


# Set up work directory
WORKDIR /var/task

# Copy application files
COPY . .

# Install Python dependencies
RUN pip install -r requirements.txt

# Run the application
CMD ["lambda_handler.lambda_handler"]