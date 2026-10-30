# Third-party software and models

AurumAI's project-owned source is licensed under AGPL-3.0-only. This does not
relicense dependencies, independently deployed services, or downloaded models.

## Application dependencies

`release/dependencies.json` inventories exact versions from the seven Python
lockfiles and three npm lockfiles. Each record carries its declared license,
lockfile and evidence hash. This inventory includes build/test dependencies;
membership does not imply that a library is included in a runtime container.

Dependency installers obtain upstream distributions and their license files.
Keep those files when redistributing a built image or a vendored distribution.
Do not replace upstream copyright notices with AurumAI attribution. The package's
own notice is authoritative when metadata abbreviates or omits a license.

The npm package `khroma` omits the license field in its package metadata; its
distributed `license` file contains the MIT grant and original copyright notice.

## Independent container services

Compose and Helm connect to independent infrastructure. PostgreSQL/pgvector,
Keycloak, Temporal, ClickHouse, MinIO, Langfuse, etcd, Milvus, Bifrost, SearXNG and
Infinity are not relicensed by AurumAI. Their component licenses and image-layer
notices apply independently; an image can contain multiple differently licensed
components. Application-image publication requires its own image/SBOM review.

Redis licensing depends on its release series. Redis 7.2 and earlier use
BSD-3-Clause; Redis 7.4 through 7.8 use RSALv2/SSPLv1; Redis 8 adds AGPLv3 as an
available option. See the [upstream licensing history](https://redis.io/legal/licenses/).
Review the exact image used by a deployment rather than inferring its license
from the product name or a floating major tag.

## Downloaded models and optional services

Embedding, reranking, document parsing and LLM providers can obtain separately
licensed model weights or call services with their own terms. Verify the exact
model revision and upstream model card before distributing weights or offering
a hosted deployment. Provider API credentials and account IDs are supplied by
the operator and are not part of this source release.

## Source assets

Preserve notices for third-party icons, fonts, generated bundles and sample skill
dependencies. Sample skill ZIPs are reproducible source bundles, not an exception
to license or secret review. AurumAI does not publish its private design-tool
cache, internal review history, or operator recovery records.

The `hermes-sandbox` image installs Hermes Agent (MIT, Nous Research) from its
release's source archive on GitHub, verified by hash, with its LICENSE, and its
dependencies from PyPI, pinned by hash in `hermes-sandbox/requirements.txt`; their
installed distributions carry their own license files. All of them are under
permissive licenses (MIT, BSD, Apache-2.0, ISC, PSF) except `certifi` and
`pathspec` (MPL-2.0).
The data libraries in `hermes-sandbox/requirements-data.txt` (matplotlib,
pandas, openpyxl, numpy and what they need) are permissive as well: the
Matplotlib license (PSF-based), BSD, MIT. Matplotlib bundles the DejaVu (Bitstream
Vera license) and STIX (SIL Open Font License) fonts, and numpy's wheels bundle
OpenBLAS (BSD) and the GCC Fortran runtime (GPL-3.0 with the GCC Runtime Library
Exception); each distribution carries these licenses.

The vendored Temporal setup/namespace/dynamic-configuration samples retain their
MIT license and original Temporal Technologies copyright. See
[the file-specific notice](temporal/THIRD_PARTY_NOTICES.md).
