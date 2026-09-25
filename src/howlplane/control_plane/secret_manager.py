import json
import os
import sys
from typing import Optional

try:
    import boto3
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


class SecretManager:
    """
    Handles secret retrieval from AWS Secrets Manager with a fallback
    to local environment variables (for local development).
    """

    def __init__(self, region_name: str = "us-east-1"):
        self.region_name = region_name
        self._client = None

        if BOTO3_AVAILABLE and os.environ.get("USE_AWS_SECRETS_MANAGER") == "true":
            try:
                self._client = boto3.client(
                    service_name="secretsmanager", region_name=self.region_name
                )
            except Exception as e:
                print(
                    f"Warning: Failed to initialize AWS Secrets Manager client: {e}",
                    file=sys.stderr,
                )

    def get_secret(self, secret_name: str, key: Optional[str] = None) -> Optional[str]:
        """
        Retrieves a secret. If AWS Secrets Manager is configured, tries to pull it from there.
        Falls back to local environment variables.

        Args:
            secret_name: The name of the secret in AWS or the ENV var name
            key: If the AWS secret is a JSON string, the key to extract.
        """
        if self._client:
            try:
                response = self._client.get_secret_value(SecretId=secret_name)
                if "SecretString" in response:
                    secret_data = response["SecretString"]
                    if key:
                        try:
                            parsed_secret = json.loads(secret_data)
                            return parsed_secret.get(key)
                        except json.JSONDecodeError:
                            print(
                                f"Warning: Secret {secret_name} is not valid JSON.",
                                file=sys.stderr,
                            )
                            return secret_data
                    return secret_data
            except ClientError as e:
                print(
                    f"AWS Secrets Manager Error retrieving {secret_name}: {e}",
                    file=sys.stderr,
                )

        # Fallback to local environment variable
        fallback_key = key if key else secret_name
        return os.environ.get(fallback_key)
