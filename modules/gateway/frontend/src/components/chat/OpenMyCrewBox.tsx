import { useRef, useState } from 'react';
import { openDevBoxTab } from '@/services/devBoxOpen';

/**
 * Chat action for the owner's EC2 Crew box. The click synchronously opens the interstitial
 * tab (`pages/settings/DevBoxOpen`), which runs the one-click flow (#8140: ensure/start →
 * enroll this host automatically when needed → wait for Crew → mint the ticket) and then
 * becomes the Crew dashboard. A tab opened inside the click is never a pop-up; chat stays
 * open here. If a blocker still refuses, the interstitial href is offered as a visible link.
 * The chat action makes no API call itself.
 */
export default function OpenMyCrewBox() {
  const [blockedHref, setBlockedHref] = useState<string | null>(null);
  const openedTab = useRef<Window | null>(null);
  const open = () => {
    if (openedTab.current && !openedTab.current.closed) { openedTab.current.focus(); return; }
    const { tab, href } = openDevBoxTab();
    openedTab.current = tab;
    setBlockedHref(tab ? null : href);
  };
  return <section aria-label="EC2 Crew">
    <button type="button" onClick={open}>Open my EC2 Crew</button>
    {blockedHref && <p role="alert">The browser blocked the new tab.{' '}
      <a href={blockedHref} target="_blank">Open in new tab</a></p>}
  </section>;
}
