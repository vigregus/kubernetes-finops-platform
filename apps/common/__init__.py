"""Code shared by the workload services.

Lives outside any single service directory, so the Docker build context for
each service is the repository root (see scripts/build-workload-images.sh
and apps/*/Dockerfile) rather than the service directory itself.
"""
