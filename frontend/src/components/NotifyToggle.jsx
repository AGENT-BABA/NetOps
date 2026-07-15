import { useEffect, useState } from "react";
import { Bell, BellOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  notificationsSupported, notificationPermission,
  requestNotificationPermission, NOTIF_ENABLED_KEY,
} from "@/lib/notify";
import { initFCM } from "@/lib/fcm";
import { toast } from "sonner";

export function NotifyToggle() {
  const [perm, setPerm] = useState(notificationPermission());
  const [enabled, setEnabled] = useState(
    typeof window !== "undefined" && localStorage.getItem(NOTIF_ENABLED_KEY) !== "off"
  );

  useEffect(() => { setPerm(notificationPermission()); }, []);

  if (!notificationsSupported()) return null;

  const handle = async () => {
    if (perm === "default") {
      const p = await requestNotificationPermission();
      setPerm(p);
      if (p === "granted") {
        localStorage.setItem(NOTIF_ENABLED_KEY, "on");
        setEnabled(true);
        toast.success("Push notifications enabled");
        initFCM();
      } else if (p === "denied") {
        toast.error("Notifications blocked. Enable them from your browser site settings.");
      }
      return;
    }
    if (perm === "denied") {
      toast.error("Notifications are blocked. Open site settings to allow.");
      return;
    }
    // granted → toggle enabled
    const next = !enabled;
    setEnabled(next);
    localStorage.setItem(NOTIF_ENABLED_KEY, next ? "on" : "off");
    toast(next ? "Push notifications ON" : "Push notifications OFF");
    if (next) initFCM();
  };

  const active = perm === "granted" && enabled;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9"
            onClick={handle}
            data-testid="push-toggle-btn"
            aria-label={active ? "Turn off push notifications" : "Turn on push notifications"}
          >
            {active ? <Bell className="h-4 w-4 text-primary" /> : <BellOff className="h-4 w-4 text-muted-foreground" />}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="text-xs">
          {perm === "granted"
            ? enabled ? "Push ON — click to disable" : "Push OFF — click to enable"
            : perm === "denied" ? "Blocked · check browser settings" : "Enable push notifications"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
