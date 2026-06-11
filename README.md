## Overview

**CoreSight Identity Provider (csr-idp)** is a WordPress plugin that transforms WordPress site into a standards-compliant Identity Provider (IdP). It allows internal or external applications to authenticate users, providing endpoints for authorization, token issuance, user info, and JSON Web Key Sets (JWKS). The plugin is designed for extensibility, security, and easy integration.

---

## File-by-File Explanation

### Root Files

- **csr-idp.php**  
  Main plugin bootstrap file.  
  - Defines constants.
  - Loads the autoloader.
  - Registers activation/deactivation hooks.
  - Boots the plugin via `CoreSight\IdP\Core`.
  - Loads WP-CLI commands if in CLI mode.

- **uninstall.php**  
  Handles cleanup when the plugin is deleted (removes options, tables, etc.).

- **index.php**  
  Prevents directory listing.

- **composer.json**  
  Composer dependencies (e.g., `firebase/php-jwt` for JWT handling).

---

### `includes/` — Main Plugin Logic

#### Core and CLI

- **class-core.php**  
  The main orchestrator.  
  - Registers REST endpoints.
  - Loads textdomain for translations.
  - Hooks admin UI.
  - Handles plugin initialization and activation.

- **class-wp-cli-script.php**  
  Adds WP-CLI commands for managing clients, keys, and tokens.

#### Admin UI

- **class-admin-ui.php**  
  - Registers admin menu pages.
  - Handles admin actions (add/edit/delete clients, rotate keys).
  - Renders admin pages (keys, logs, clients).

- **admin/assets/admin-clients.js**  
  JavaScript for admin client management UI (dynamic form fields, etc.).

- **admin/views/class-add-clients.php**  
  View for adding/editing clients in the admin.

- **admin/views/class-logs-table.php**  
  View for displaying logs in the admin.

- **admin/views/class-manage-clients.php**  
  View for listing and managing all registered clients.

#### Authentication

- **auth/class-idp-flow.php**  
  Core logic for the authentication flow (authorization, token, etc.).

- **auth/class-session-manager.php**  
  Handles session storage for flows (e.g., state, nonce, PKCE).

#### Database

- **db/class-client-repository.php**  
  CRUD operations for clients (create, read, update, delete).

- **db/class-db-migration.php**  
  Handles database schema creation and updates (tables for clients, keys, logs).

#### Endpoints

Implements the endpoints:

- **class-authorize-endpoint.php**  
  Handles `/authorize` requests (user authentication, consent, code issuance).

- **class-token-endpoint.php**  
  Handles `/token` requests (exchanges code for tokens, refresh, etc.).

- **endpoints/class-userinfo-endpoint.php**  
  Handles `/userinfo` requests (returns user claims).

- **endpoints/class-jwks-endpoint.php**  
  Serves the JWKS (public keys for JWT verification).

- **endpoints/class-logout-endpoint.php**  
  Handles logout requests.

  [More on endpoints](#endpoints-brief)

#### Helpers

- **helpers/autoloader.php**  
  PSR-4 autoloader for plugin classes.

#### JWT Handling

- **jwt/class-jwk-set.php**  
  Manages the JWKS (public keys for clients).

- **jwt/class-jwt-signer.php**  
  Handles JWT signing and verification.

- **jwt/class-keystore.php**  
  Manages key pairs (rotation, storage, retrieval).

- **jwt/class-token-generator.php**  
  Generates JWTs (ID tokens, access tokens).

#### Logging

- **logging/class-logger.php**  
  Writes logs (authentication events, errors, etc.).

- **class-admin-logs-ui.php**  
  Renders logs in the admin UI.

#### Security

- **security/class-pkce-validator.php**  
  Validates PKCE (Proof Key for Code Exchange) for clients.

- **security/class-redirect-validator.php**  
  Validates redirect URIs for clients.

---

### `vendor/` — Third-Party Libraries

- **firebase/php-jwt/**  
  Library for encoding/decoding JWTs, validating signatures, etc.

---

## Plugin Flow

1. **Initialization**  
   - csr-idp.php loads the autoloader and boots `Core`.
   - Registers endpoints and admin menus.

2. **Admin UI**  
   - Admins can manage clients, rotate keys, and view logs via the WordPress admin.
   - UI logic is in `Admin_UI` and its views.

3. **Endpoints**  
   - `/authorize` → `Authorize_Endpoint`: Handles login, consent, and issues authorization codes.
   - `/token` → `Token_Endpoint`: Exchanges codes for tokens, issues JWTs.
   - `/userinfo` → `Userinfo_Endpoint`: Returns user claims.
   - `/jwks` → `JWKS_Endpoint`: Returns public keys for JWT validation.
   - `/logout` → `Logout_Endpoint`: Handles logout.

   [More on endpoints](#endpoints-brief)

4. **JWT Handling**  
   - JWTs are signed using keys managed by `Keystore`.
   - JWKS endpoint exposes public keys for clients to verify tokens.

5. **Security**  
   - PKCE and redirect URI validation for secure flows.
   - Logs all authentication and admin actions.

6. **WP-CLI**  
   - Manage clients, keys, and tokens via CLI using `class-wp-cli-script.php`.

---

## Key Classes and Their Roles

- `CoreSight\IdP\Core`: Main orchestrator.
- `CoreSight\IdP\Admin\Admin_UI`: Admin interface.
- `CoreSight\IdP\DB\Client_Repository`: Client storage.
- `CoreSight\IdP\JWT\Keystore`: Key management.
- `CoreSight\IdP\JWT\Token_Generator`: JWT creation.
- `CoreSight\IdP\Endpoints\Authorize_Endpoint`: `/authorize` logic.
- `CoreSight\IdP\Endpoints\Token_Endpoint`: `/token` logic.
- `CoreSight\IdP\Endpoints\Userinfo_Endpoint`: `/userinfo` logic.
- `CoreSight\IdP\Endpoints\JWKS_Endpoint`: `/jwks` logic.
- `CoreSight\IdP\Logging\Logger`: Logging.

---


## Endpoints Brief

### 1. `/csr-idp/authorize`

**Type:**
Frontend (GET, browser-initiated)

**Purpose:**
Initiates the OAuth2/OpenID Connect authorization code flow.

**Method:**
GET

**Headers:**

- Standard browser headers

**Query Parameters:**

- `client_id` (string, required): Registered client ID

- `redirect_uri` (string, required): Must match a registered redirect URI

- `response_type` (string, required): Must be `code`

- `state` (string, optional): Opaque value for CSRF protection

- `nonce` (string, optional): For ID token replay protection

- `code_challenge` (string, required if PKCE enforced): PKCE challenge

- `code_challenge_method` (string, optional): Usually `S256`

**Returns:**

- Redirects to `redirect_uri` with `code` and `state` on success

- Redirects with error code on failure

---

### 2. `/csr-idp/token`

**Type:** API (POST)

**Purpose:**
Exchanges an authorization code or refresh token for access, refresh, and ID tokens.

**Method:**
POST

**Headers:**

- `Content-Type: application/x-www-form-urlencoded`

- `Authorization: Basic base64(client_id:client_secret)` (if using `client_secret_basic`)

**Body Parameters:**

1. **For authorization code grant:**

  - `grant_type=authorization_code`

  - `client_id` (string, required) 

  - `code` (string, required)

  - `redirect_uri` (string, required)

  - `code_verifier` (string, required if PKCE enforced)

  - `client_secret` (string, required if using `client_secret_post`)

2. **For refresh token grant:**

  - `grant_type=refresh_token`

  - `client_id` (string, required)

  - `refresh_token` (string, required)

  - `client_secret` (string, required if using `client_secret_post`)

**Returns:**

- `200 OK` JSON:

  ```json
  {
    "access_token": "...",
    "token_type": "Bearer",
    "expires_in": 2592000,
    "refresh_token": "...",
    "id_token": "..."
  }
  ```

- `400/401` JSON error on failure

---

### 3. `/csr-idp/logout`

**Type:** Frontend/API (GET, browser-initiated)

**Purpose:**
Logs the user out of the WordPress IdP session and redirects back to the client application or WordPress home page.

This endpoint only terminates the IdP session.

The client application is responsible for clearing its own authentication cookies after redirect.

**Method:**
GET

**Query Parameters:**

- `id_token_hint` (string, required): ID token to identify session

- `post_logout_redirect_uri` (string, optional): Absolute URL to which the user is redirected after logout. No validation or client matching is performed.

- `state` (string, optional): Opaque value to maintain state

**Returns:**

- Redirects to `post_logout_redirect_uri` (if provided), or host home page

- Destroys session and cookies

---

### 4. `/wp-json/csr-idp/v1/userinfo`

**Type:** API (GET)

**Purpose:**
Returns user claims for the authenticated user.

**Method:**
GET

**Headers:**

- `Authorization: Bearer <access_token>`

**Returns:**

- `200 OK` JSON with user profile, roles, memberships, transactions

- `401/404` JSON error if token is missing, invalid, or user not found

---

### 5. `/wp-json/csr-idp/v1/jwks`

**Type:** API (GET)

**Purpose:**
Returns the public JSON Web Key Set (JWKS) for token verification.

**Method:**
GET

**Returns:**
- `200 OK` JSON:

  ```json
  {
    "keys": [
      {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "...",
        "n": "...",
        "e": "..."
      }
    ]
  }
  ```

---


## Development Notes

- **Extendability:**  
  The plugin is modular; new endpoints or authentication methods can be added by extending the relevant classes.
- **Security:**  
  All sensitive operations are permission-checked. PKCE and redirect URI validation are enforced.
- **JWT:**  
  Uses `firebase/php-jwt` for robust JWT handling.
- **Database:**  
  Uses custom tables for clients, keys, and logs. Managed via migration classes.

---

For further details, see the docblocks in each class and method.  
**Developers should start by reading `Core` and `Admin_UI` to understand the plugin's entry points and admin flow.**