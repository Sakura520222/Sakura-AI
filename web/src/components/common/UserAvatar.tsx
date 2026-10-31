import { useState } from "react";

function avatarSource(value: string | null, username: string) {
  if (value) {
    try {
      const url = new URL(value);
      if (url.protocol === "https:" && !url.username && !url.password)
        return url.href;
    } catch {
      /* Use the public GitHub avatar for an older session. */
    }
  }
  return /^[a-z\d](?:[a-z\d-]{0,37}[a-z\d])?$/i.test(username)
    ? `https://github.com/${encodeURIComponent(username)}.png?size=80`
    : undefined;
}

export function UserAvatar({
  url,
  username,
}: {
  url: string | null;
  username: string;
}) {
  const source = avatarSource(url, username);
  return (
    <AvatarImage key={source || username} source={source} username={username} />
  );
}

function AvatarImage({
  source,
  username,
}: {
  source?: string;
  username: string;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <span className="avatar" aria-hidden="true">
      {source && !failed ? (
        <img
          src={source}
          alt=""
          width={32}
          height={32}
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        username.slice(0, 1).toUpperCase() || "S"
      )}
    </span>
  );
}
