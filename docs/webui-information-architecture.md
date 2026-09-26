# WebUI navigation and route compatibility

The first navigation level is a business domain. Sidebar groups are collapsed
except for the active domain, and the sidebar is the only business-domain
navigation. Page headers keep only the current page title, description, and
page-level actions. Permissions remain on the individual routes, not only on
the navigation links.

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

The PR list is the only review-record list. Repository filters use the full
`owner/name` identity so repositories with the same name stay separate. Status,
decision, and local date range filters apply to its list, count and CSV export.
Old `/logs/`
and `/logs/list-fragment` links redirect to their `/pr/` equivalents and
preserve query parameters. Old review-detail fragment links redirect to the
full `/pr/{id}` detail. Requests from an already-open legacy HTMX page trigger
a browser-level redirect to avoid inserting a full page into a fragment.
`/logs/actions/` is the separate audit log and remains
unchanged. Route-level authentication is retained on the redirects.
