#!/bin/bash
set -e

# Configuration
IMAGE_NAME=${1:-"hkd-proxy"}
TAG=${2:-"latest"}
PLATFORMS="linux/amd64,linux/arm64"
BUILDER_NAME="multiarch-builder"

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

if ! command_exists docker; then
    echo "Error: docker is not installed."
    exit 1
fi

# Setup Buildx
echo "Checking Docker Buildx..."
if ! docker buildx version > /dev/null 2>&1; then
    echo "Error: Docker Buildx is not available. Please install it to support multi-arch builds."
    exit 1
fi

# Create/Use a builder instance
echo "Setting up builder instance '$BUILDER_NAME'..."
if ! docker buildx inspect $BUILDER_NAME > /dev/null 2>&1; then
    docker buildx create --name $BUILDER_NAME --driver docker-container --use
else
    docker buildx use $BUILDER_NAME
fi

echo "========================================"
echo "Building for platforms: $PLATFORMS"
echo "Target Image: $IMAGE_NAME:$TAG"
echo "========================================"

# Check if we should push (simple heuristic: if image name contains slash, assume registry)
# Multi-arch images typically need to be pushed to a registry to exist as a single manifest.
if [[ "$IMAGE_NAME" == *"/"* ]]; then
    echo "Registry detected in image name (contains '/')"
    echo "Running build and push..."
    docker buildx build --platform "$PLATFORMS" -t "$IMAGE_NAME:$TAG" --push .
    echo "Successfully built and pushed $IMAGE_NAME:$TAG"
else
    echo "No registry detected in image name."
    echo "Note: Multi-arch builds cannot be loaded into the local Docker daemon simultaneously as a single tag."
    echo "Building platforms individually for local usage..."

    # Split platforms by comma
    IFS=',' read -ra PLATFORM_ARRAY <<< "$PLATFORMS"

    for platform in "${PLATFORM_ARRAY[@]}"; do
        # Create a tag suffix like -linux-amd64
        safe_platform_name=$(echo "$platform" | tr '/' '-')

        echo "Building for $platform -> $IMAGE_NAME:$TAG-$safe_platform_name"
        docker buildx build --platform "$platform" -t "$IMAGE_NAME:$TAG-$safe_platform_name" --load .
    done

    echo "----------------------------------------"
    echo "Local builds complete. Available images:"
    docker images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}" | grep "$IMAGE_NAME"
    echo "----------------------------------------"
fi
