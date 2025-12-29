# TCMB EVDS Proxy

A lightweight, caching proxy server for the Central Bank of the Republic of Turkey (TCMB) Electronic Data Delivery System (EVDS) API.

This service acts as a middleware between your applications and the TCMB EVDS, providing caching to reduce API calls and a secure layer to manage access without exposing your master TCMB API key.

## Features

- **Caching**: Responses are cached for 1 hour to improve performance and reduce upstream API usage.
- **Secure Authentication**:
    - Hides your main `TCMB_API_KEY` from client applications.
    - Enforces its own authentication using `PROXY_API_KEYS`.
- **Flexible Auth Methods**: Clients can authenticate via Header (`X-API-Key`, `Authorization: Bearer`) or Query Parameter.
- **Health Checks**: Built-in `/health` endpoint for monitoring.
- **Production Ready**: Powered by Gunicorn with threaded workers.
- **Multi-Arch**: optimized for ARM64 (Raspberry Pi).

## Configuration

The container is configured using environment variables:

| Variable         | Description                                                                            | Required |
| ---------------- | -------------------------------------------------------------------------------------- | :------: |
| `TCMB_API_KEY`   | Your personal API key from TCMB EVDS.                                                  |   Yes    |
| `PROXY_API_KEYS` | Comma-separated list of keys you want to accept from clients connecting to this proxy. |   Yes    |

## Usage

### Docker CLI

```bash
docker run -d \
  --name hkd-proxy \
  -p 5000:5000 \
  -e TCMB_API_KEY="your-tcmb-service-key" \
  -e PROXY_API_KEYS="client-key-1,client-key-2" \
  yourusername/hkd-proxy:latest
```

### Docker Compose

```yaml
version: "3.8"
services:
    hkd-proxy:
        image: yourusername/hkd-proxy:latest
        ports:
            - "5000:5000"
        environment:
            - TCMB_API_KEY=your_tcmb_api_key_here
            - PROXY_API_KEYS=secret_key_for_your_clients,another_secret
        restart: unless-stopped
```

## API Endpoints

### 1. Proxy Endpoint: `/tcmb`

Forwards requests to the EVDS service.

**Request:**
`GET /tcmb?series=TP.DK.USD.A&startDate=01-01-2023&endDate=01-02-2023`

**Authentication (Choose one):**

- Header: `X-API-Key: client-key-1`
- Header: `Authorization: Bearer client-key-1`
- Query Param: `?api_key=client-key-1`

### 2. Health Check: `/health`

Returns the status of the service.

**Request:**
`GET /health`

**Response:**

```json
{
    "status": "healthy",
    "tcmb_api_configured": true,
    "proxy_auth_configured": true
}
```

## License

MIT
