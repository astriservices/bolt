
function boltEscape(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[c]));
}

async function boltNav() {
    const nav = document.querySelector('nav');
    if (!nav) return;

    let links = nav.querySelector('.nav-links');

    if (!links) {
        links = document.createElement('div');
        links.className = 'nav-links';
        nav.append(links);
    }

    try {
        const r = await fetch('/api/me', { cache: 'no-store' });

        if (!r.ok) {
            links.innerHTML =
                '<a href="/usage">Workspace</a>' +
                '<a href="/buy">Credits</a>' +
                '<a href="/redeem">Redeem</a>' +
                '<a href="/login">Log in</a>' +
                '<a class="btn small" href="/signup">Create account</a>';
            return;
        }

        const u = await r.json();

        links.innerHTML =
            '<a href="/redeem">Redeem</a>' +
            '<a href="/usage">Workspace</a>' +
            '<a href="/buy">Credits</a>' +
            (u.is_admin ? '<a href="/admin">Admin</a>' : '') +
            '<span class="nav-account">' +
                '<span class="avatar">' +
                    boltEscape(u.username.slice(0, 1).toUpperCase()) +
                '</span>' +
                '<span>' +
                    '<span class="account-name">' +
                        boltEscape(u.username) +
                    '</span>' +
                    '<span class="account-credit">' +
                        u.credits + ' credits' +
                    '</span>' +
                '</span>' +
            '</span>' +
            '<a href="/logout">Log out</a>';

    } catch {
        links.innerHTML =
            '<a href="/redeem">Redeem</a>' +
            '<a href="/login">Log in</a>' +
            '<a href="/signup">Create account</a>';
    }
}

document.addEventListener('DOMContentLoaded', boltNav);