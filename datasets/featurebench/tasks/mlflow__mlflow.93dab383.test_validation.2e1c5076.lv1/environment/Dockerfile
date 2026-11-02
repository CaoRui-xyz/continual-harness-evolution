FROM docker.io/libercoders/featurebench-specs_mlflow-instance_efffa87e
COPY mask.patch /opt/featurebench/mask.patch
RUN source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && rm -rf /testbed/* && cp -r /root/my_repo/* /testbed/ && rm -rf /root/my_repo
RUN cd /testbed && git apply --whitespace=fix /opt/featurebench/mask.patch && rm -f /opt/featurebench/mask.patch
RUN rm -f /testbed/tests/genai/scorers/test_validation.py
RUN cd /testbed && rm -rf .git && git init && git config user.email "fb@bench.com" && git config user.name "FeatureBench" && git add -A && git commit -m "Initial commit for FeatureBench evaluation" --allow-empty
WORKDIR /testbed
