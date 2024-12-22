# Use the PyTorch image as the base image
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-devel

# Set environment variables for CUDA
ENV PATH="/usr/local/cuda/bin:$PATH" \
    CUDA_HOME="/usr/local/cuda" \
    LD_LIBRARY_PATH="/usr/local/cuda/lib64:$LD_LIBRARY_PATH"

# Set the working directory inside the container
WORKDIR /workspace/Matte-Anything

# Update the package list, install required system libraries, and clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    libglib2.0-0 \
    libgl1-mesa-glx \
    net-tools \
    curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python dependencies
RUN pip install --upgrade pip && \
    pip install git+https://github.com/facebookresearch/segment-anything.git && \
    python -m pip install 'git+https://github.com/facebookresearch/detectron2.git'

# Copy and install additional Python requirements
COPY requirements.txt /workspace/Matte-Anything/
RUN pip install -r requirements.txt

RUN git clone https://github.com/YihanHu-2022/DiffMatte.git && \
    git clone https://github.com/aipixel/AEMatter.git

# Clone and install GroundingDINO
RUN git clone https://github.com/IDEA-Research/GroundingDINO.git /workspace/Matte-Anything/GroundingDINO && \
    pip install -e /workspace/Matte-Anything/GroundingDINO

# Expose the application port
EXPOSE 7860

# Default command
CMD ["bash"]
