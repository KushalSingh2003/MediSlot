import requests

from flask import current_app, session


class APIClient:

    @staticmethod
    def base_url():
        return current_app.config["API_BASE_URL"].rstrip("/")

    @staticmethod
    def get_headers(authenticated=True):

        headers = {
            "Content-Type": "application/json"
        }

        access_token = session.get("access_token")

        if authenticated and access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        return headers

    @staticmethod
    def refresh_access_token():

        refresh_token = session.get("refresh_token")
        current_app.logger.info("Access token expired. Attempting refresh...")

        if not refresh_token:
            return False

        try:
            response = requests.post(
                f"{APIClient.base_url()}/auth/refresh",
                json={"refresh_token": refresh_token},
                headers={"Content-Type": "application/json"},
                timeout=20
            )

            if response.status_code != 200:
                current_app.logger.warning(
                    "Token refresh rejected. status=%s",
                    response.status_code
                )
                return False

            data = response.json()
            access_token = data.get("access_token")

            if not access_token:
                current_app.logger.error(
                    "Refresh response did not contain access_token."
                )
                return False

            session["access_token"] = access_token
            current_app.logger.info("Access token refreshed successfully.")

            # Your current refresh response does not return a new
            # refresh_token, so retain the existing one.
            if data.get("refresh_token"):
                session["refresh_token"] = data["refresh_token"]

            return True

        except requests.RequestException:
            current_app.logger.exception("Token refresh request failed.")
            return False
    @staticmethod
    def request(method, endpoint, refresh_on_401=True, **kwargs):

        url = f"{APIClient.base_url()}/{endpoint.lstrip('/')}"

        custom_headers = kwargs.pop("headers", {})
        headers = APIClient.get_headers()
        headers.update(custom_headers)

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                timeout=20,
                **kwargs
            )

            if (
                response.status_code == 401
                and refresh_on_401
                and session.get("refresh_token")
            ):
                current_app.logger.warning(
                    "API returned 401. Checking refresh token..."
                )

                if APIClient.refresh_access_token():

                    headers = APIClient.get_headers()
                    headers.update(custom_headers)

                    response = requests.request(
                        method=method,
                        url=url,
                        headers=headers,
                        timeout=20,
                        **kwargs
                    )

                else:
                    session.clear()

            return response

        except requests.RequestException:
            current_app.logger.exception(
                "API Gateway request failed. endpoint=%s",
                endpoint
            )
            raise
