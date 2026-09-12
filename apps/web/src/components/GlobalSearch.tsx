import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

/**
 * The search box in the app shell.
 *
 * Deliberately dumb: it takes a term and navigates to the search page. It runs no query of
 * its own and holds no results, so there is exactly one place in the app that knows how
 * search works — the alternative is a dropdown here that slowly grows a second, divergent
 * implementation of the results list.
 *
 * `/` focuses it, the convention every console and code host has settled on. The guard
 * against firing while an input already has focus is what stops the shortcut from eating a
 * slash someone is typing into a filter field.
 */
export function GlobalSearch() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;

      const active = document.activeElement;
      const typing =
        active instanceof HTMLInputElement ||
        active instanceof HTMLTextAreaElement ||
        (active instanceof HTMLElement && active.isContentEditable);
      if (typing) return;

      event.preventDefault();
      inputRef.current?.focus();
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const term = value.trim();
    if (term.length < 2) return;
    navigate(`/dashboard/search?q=${encodeURIComponent(term)}`);
  }

  return (
    <form className="global-search" onSubmit={submit} role="search">
      <span className="search-icon" aria-hidden="true">
        ⌕
      </span>
      <input
        ref={inputRef}
        type="search"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="Search a company or domain…"
        aria-label="Search companies and domains"
      />
      <kbd aria-hidden="true">/</kbd>
    </form>
  );
}
