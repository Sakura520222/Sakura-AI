# WebUI navigation and route compatibility

The first navigation level is a business domain. The sidebar expands to show
its features, and pages in the same domain share contextual tabs. Permissions
remain on the individual routes, not only on the navigation links.

| Domain | Existing routes | Visibility |
| --- | --- | --- |
| Reviews & Analysis | `/pr/`, `/issues/`, `/queue/` | Queue: admin and super admin |
| Repositories | `/github-app/`, `/repos/`, `/scans/`, `/sakura-memory/`, `/vector-db/` | GitHub App: signed-in users; repository list and scans: admin; memory and vector data: super admin |
| Agent | `/agent-team/`, `/agent-skills/` | Skills: super admin |
| Repository Aid | `/star-aid/` | Signed-in users |
| Observability | `/activity/observability/`, `/logs/actions/` | Audit: admin and super admin |
| Billing | `/billing/`, `/billing/admin/plans`, `/billing/admin/codes`, `/billing/admin/refund-requests` | Shown when payment is enabled; admin pages: super admin |
| Administration | `/users/`, `/security/` | Users: admin; security: super admin |
| Settings | `/settings/`, `/config`, `/config/ai`, `/system-config/`, `/config/backup` | Personal: signed-in users; remaining pages: super admin |

The dashboard and Repository Aid are direct entries. Announcements are reached
from the top bar, and About from the account menu. Existing feature routes
remain valid while the interface is reorganized.

The PR list is the only review-record list. Repository, status, decision, and
local date range filters apply to its list, count and CSV export. Old `/logs/`
and `/logs/list-fragment` links redirect to their `/pr/` equivalents and
preserve query parameters. Old review-detail fragment links redirect to the
full `/pr/{id}` detail. `/logs/actions/` is the separate audit log and remains
unchanged. Route-level authentication is retained on the redirects.
