import { useCallback, useEffect, useMemo, useState } from "react";

type Platform = "youtube" | "telegram" | "instagram" | "facebook";
type PromptPlatform = "youtube" | "telegram";
const publishingPlatforms: Platform[] = ["youtube", "telegram", "instagram", "facebook"];
const drivePublishingPlatforms: Exclude<Platform, "telegram">[] = ["youtube", "instagram", "facebook"];

type Video = {
  id: string;
  drive_file_id: string;
  filename: string;
  thumbnail?: string;
  size: number;
  duration?: number | null;
  created_at?: string;
  status: string;
};

type SchedulePlatform = {
  date?: string | null;
  time?: string | null;
  status?: string | null;
};

type Schedule = {
  schedule_id: string;
  video_id: string;
  filename: string;
  thumbnail: string;
  platforms: string[];
  youtube: SchedulePlatform;
  telegram: SchedulePlatform;
  instagram: SchedulePlatform;
  facebook: SchedulePlatform;
  facebook_page_url?: string | null;
  timezone: string;
  status: string;
  created_at: string;
  updated_at: string;
  last_error?: string;
};

type YouTubeChannel = {
  title: string;
  thumbnail?: string;
};

type WorkerHealth = {
  running: boolean;
  last_checked_at?: string | null;
  last_error?: string | null;
};

type AutomationSettings = {
  enabled: boolean;
  platforms: PromptPlatform[];
  timezone: string;
  youtube_times: string[];
  telegram_times: string[];
};

type DriveAutomationSettings = {
  enabled: boolean;
  interval_minutes: number;
  max_videos_per_day: number;
  platforms: Platform[];
  timezone: string;
  facebook_page_url: string;
};

type SocialSession = {
  platform: "instagram" | "facebook";
  authenticated: boolean;
  browser_open: boolean;
  profile_saved: boolean;
};

type AutomationJob = {
  job_id: string;
  prompt: string;
  status: string;
  stage: string;
  progress_percent: number;
  created_at: string;
  generation_id?: string;
  drive_filename?: string;
  drive_url?: string;
  idea_provider?: string;
  review_provider?: string;
  last_error?: string;
};

type ScheduleForm = {
  videoId: string;
  platforms: Platform[];
  youtubeDate: string;
  youtubeTime: string;
  telegramDate: string;
  telegramTime: string;
  instagramDate: string;
  instagramTime: string;
  facebookDate: string;
  facebookTime: string;
  facebookPageUrl: string;
  timezone: string;
};

const defaultForm = (videoId = ""): ScheduleForm => ({
  videoId,
  platforms: ["youtube"],
  youtubeDate: "",
  youtubeTime: "",
  telegramDate: "",
  telegramTime: "",
  instagramDate: "",
  instagramTime: "",
  facebookDate: "",
  facebookTime: "",
  facebookPageUrl: "",
  timezone: "Asia/Karachi",
});

const defaultAutomationSettings: AutomationSettings = {
  enabled: true,
  platforms: ["youtube"],
  timezone: "Asia/Karachi",
  youtube_times: ["17:30", "21:30"],
  telegram_times: ["17:30", "21:30"],
};

const defaultDriveAutomationSettings: DriveAutomationSettings = {
  enabled: false,
  interval_minutes: 30,
  max_videos_per_day: 3,
  platforms: ["youtube"],
  timezone: "Asia/Karachi",
  facebook_page_url: "",
};

function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item) {
          return String(item.msg);
        }
        return "";
      })
      .filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  if (detail && typeof detail === "object") {
    if ("detail" in detail) return apiErrorMessage(detail.detail, fallback);
    if ("msg" in detail) return String(detail.msg);
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

function formatDateLabel(value?: string | null) {
  if (!value) return "Not set";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split("-").map(Number);
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" })
      .format(new Date(year, month - 1, day));
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function formatTimeLabel(value?: string | null) {
  if (!value) return "Not set";
  return value;
}

function formatDuration(value?: number | null) {
  if (!value) return "Unknown duration";
  const totalSeconds = Math.floor(value / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function SocialPlatformIcon({
  platform,
  className,
}: {
  platform: "instagram" | "facebook";
  className: string;
}) {
  if (platform === "instagram") {
    return (
      <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
        <rect x="3.25" y="3.25" width="17.5" height="17.5" rx="5" />
        <circle cx="12" cy="12" r="4.1" />
        <circle cx="17.7" cy="6.7" r="1" className="instagram-dot" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M13.8 21v-8h2.7l.4-3.1h-3.1v-2c0-.9.3-1.5 1.6-1.5H17V3.6c-.3 0-1.3-.1-2.5-.1-2.5 0-4.2 1.5-4.2 4.3v2.1H7.5V13h2.8v8h3.5Z" />
    </svg>
  );
}

function ThumbnailImage({ src, alt, fallback }: { src?: string; alt: string; fallback: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <span className="thumbnail-fallback" role={alt ? "img" : undefined} aria-label={alt || undefined}>
        {fallback}
      </span>
    );
  }
  return <img src={src} alt={alt} onError={() => setFailed(true)} />;
}

function hasDeliveredPlatform(schedule: Schedule) {
  return schedule.platforms.some((platform) => {
    const delivery = schedule[platform as Platform];
    return delivery?.status === "published" || delivery?.status === "sent";
  });
}

function getPlatformSchedule(schedule: Schedule, platform: Platform) {
  return schedule[platform];
}

function scheduleTimeFromDraft(draft: ScheduleForm, platform: Platform) {
  switch (platform) {
    case "youtube":
      return { date: draft.youtubeDate, time: draft.youtubeTime };
    case "telegram":
      return { date: draft.telegramDate, time: draft.telegramTime };
    case "instagram":
      return { date: draft.instagramDate, time: draft.instagramTime };
    case "facebook":
      return { date: draft.facebookDate, time: draft.facebookTime };
  }
}

function Badge({ text, tone = "neutral" }: { text: string; tone?: string }) {
  return <span className={`badge badge-${tone}`}>{text}</span>;
}

function App() {
  const [videos, setVideos] = useState<Video[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [channel, setChannel] = useState<YouTubeChannel | null>(null);
  const [workerHealth, setWorkerHealth] = useState<WorkerHealth>({ running: false });
  const [automationHealth, setAutomationHealth] = useState<WorkerHealth>({ running: false });
  const [driveAutomationHealth, setDriveAutomationHealth] = useState<WorkerHealth>({ running: false });
  const [activeSection, setActiveSection] = useState("Dashboard");
  const [selectedVideoId, setSelectedVideoId] = useState<string>("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ScheduleForm>(defaultForm());
  const [error, setError] = useState<string>("");
  const [modalError, setModalError] = useState<string>("");
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [loading, setLoading] = useState(false);
  const [automationSettings, setAutomationSettings] = useState<AutomationSettings>(defaultAutomationSettings);
  const [driveAutomationSettings, setDriveAutomationSettings] = useState<DriveAutomationSettings>(defaultDriveAutomationSettings);
  const [savedDriveAutomationSettings, setSavedDriveAutomationSettings] = useState<DriveAutomationSettings>(defaultDriveAutomationSettings);
  const [editingDriveAutomationSettings, setEditingDriveAutomationSettings] = useState(false);
  const [socialSessions, setSocialSessions] = useState<Record<"instagram" | "facebook", SocialSession>>({
    instagram: { platform: "instagram", authenticated: false, browser_open: false, profile_saved: false },
    facebook: { platform: "facebook", authenticated: false, browser_open: false, profile_saved: false },
  });
  const [socialError, setSocialError] = useState("");
  const [socialNotice, setSocialNotice] = useState("");
  const [socialBusy, setSocialBusy] = useState<"instagram" | "facebook" | null>(null);
  const [automationJobs, setAutomationJobs] = useState<AutomationJob[]>([]);
  const [automationPrompts, setAutomationPrompts] = useState("");
  const [automationError, setAutomationError] = useState("");
  const [automationNotice, setAutomationNotice] = useState("");
  const [driveAutomationError, setDriveAutomationError] = useState("");
  const [driveAutomationNotice, setDriveAutomationNotice] = useState("");
  const [savingAutomation, setSavingAutomation] = useState(false);
  const [savingDriveAutomation, setSavingDriveAutomation] = useState(false);

  const loadAutomation = useCallback(async (includeSettings = false) => {
    const response = await fetch("/api/automation");
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(data?.detail ?? "Unable to load automation settings");
    }
    if (includeSettings) setAutomationSettings(data.settings as AutomationSettings);
    setAutomationJobs(data.jobs as AutomationJob[]);
  }, []);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [videosRes, schedulesRes] = await Promise.all([
        fetch("/api/videos"),
        fetch("/api/schedules"),
      ]);
      const videosData = videosRes.ok
        ? ((await videosRes.json()) as Video[])
        : null;
      const schedulesData = schedulesRes.ok
        ? ((await schedulesRes.json()) as Schedule[])
        : null;
      if (!videosRes.ok) {
        const data = await videosRes.json().catch(() => null);
        throw new Error(data?.detail ?? `Unable to load videos (${videosRes.status})`);
      }
      if (!schedulesRes.ok) {
        const data = await schedulesRes.json().catch(() => null);
        throw new Error(data?.detail ?? `Unable to load schedules (${schedulesRes.status})`);
      }
      setVideos(videosData);
      setSchedules(schedulesData);
      setError("");
      if (videosData[0]) {
        setSelectedVideoId((current) => current || videosData[0].id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const refresh = window.setInterval(() => void loadData(), 30000);
    return () => window.clearInterval(refresh);
  }, [loadData]);

  useEffect(() => {
    const refreshAutomation = async () => {
      try {
        await loadAutomation();
      } catch (err) {
        setAutomationError(err instanceof Error ? err.message : "Unable to load automation");
      }
    };
    void loadAutomation(true).catch((err: unknown) => {
      setAutomationError(err instanceof Error ? err.message : "Unable to load automation");
    });
    const refresh = window.setInterval(() => void refreshAutomation(), 15000);
    return () => window.clearInterval(refresh);
  }, [loadAutomation]);

  useEffect(() => {
    void fetch("/api/drive-automation/settings")
      .then(async (response) => {
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(apiErrorMessage(data?.detail, "Unable to load Drive auto-publishing settings"));
        }
        setDriveAutomationSettings(data as DriveAutomationSettings);
        setSavedDriveAutomationSettings(data as DriveAutomationSettings);
      })
      .catch((err: unknown) => {
        setDriveAutomationError(err instanceof Error ? err.message : "Unable to load Drive auto-publishing settings");
      });
  }, []);

  const refreshSocialSessions = useCallback(async () => {
    const results = await Promise.all(
      (["instagram", "facebook"] as const).map(async (platform) => {
        const response = await fetch(`/api/social-sessions/${platform}`);
        const data = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(data?.detail ?? `Unable to check ${platform} login`);
        }
        return [platform, data as SocialSession] as const;
      }),
    );
    setSocialSessions((current) => ({ ...current, ...Object.fromEntries(results) }));
  }, []);

  useEffect(() => {
    void refreshSocialSessions().catch((err: unknown) => {
      setSocialError(err instanceof Error ? err.message : "Unable to check social logins");
    });
  }, [refreshSocialSessions]);

  useEffect(() => {
    void fetch("/api/youtube/channel")
      .then(async (response) => {
        if (!response.ok) return null;
        return (await response.json()) as YouTubeChannel;
      })
      .then((data) => setChannel(data))
      .catch(() => setChannel(null));
  }, []);

  useEffect(() => {
    const refreshHealth = async () => {
      try {
        const response = await fetch("/api/health");
        if (!response.ok) throw new Error(`API health request failed (${response.status})`);
        const data = (await response.json()) as {
          scheduler?: WorkerHealth;
          automation?: WorkerHealth;
          drive_automation?: WorkerHealth;
        };
        if (data.scheduler) setWorkerHealth(data.scheduler);
        if (data.automation) setAutomationHealth(data.automation);
        if (data.drive_automation) setDriveAutomationHealth(data.drive_automation);
      } catch (err) {
        setWorkerHealth({
          running: false,
          last_error: err instanceof Error ? err.message : "API health request failed",
        });
      }
    };
    void refreshHealth();
    const refresh = window.setInterval(() => void refreshHealth(), 10000);
    return () => window.clearInterval(refresh);
  }, []);

  const stats = useMemo(() => {
    const youtubeCount = schedules.filter((schedule) => schedule.platforms.includes("youtube")).length;
    const telegramCount = schedules.filter((schedule) => schedule.platforms.includes("telegram")).length;
    const instagramCount = schedules.filter((schedule) => schedule.platforms.includes("instagram")).length;
    const facebookCount = schedules.filter((schedule) => schedule.platforms.includes("facebook")).length;
    return [
      { label: "Total scheduled", value: String(schedules.length), tone: "purple", icon: "clock" },
      { label: "YouTube", value: String(youtubeCount), tone: "red", icon: "youtube" },
      { label: "Telegram", value: String(telegramCount), tone: "blue", icon: "telegram" },
      { label: "Instagram", value: String(instagramCount), tone: "purple", icon: "instagram" },
      { label: "Facebook", value: String(facebookCount), tone: "blue", icon: "facebook" },
      { label: "Drive videos", value: String(videos.length), tone: "green", icon: "drive" },
    ];
  }, [schedules, videos]);

  const openCreateModal = () => {
    setEditingScheduleId(null);
    const first = videos.find((video) => video.id === selectedVideoId)?.id || videos[0]?.id || "";
    setDraft({
      ...defaultForm(first),
      facebookPageUrl: driveAutomationSettings.facebook_page_url,
    });
    setSelectedVideoId(first);
    setError("");
    setModalError("");
    setModalOpen(true);
  };

  const openEditModal = (schedule: Schedule) => {
    setEditingScheduleId(schedule.schedule_id);
    setDraft({
      videoId: schedule.video_id,
      platforms: (schedule.platforms as Platform[]) ?? ["youtube"],
      youtubeDate: schedule.youtube?.date ?? "",
      youtubeTime: schedule.youtube?.time ?? "",
      telegramDate: schedule.telegram?.date ?? "",
      telegramTime: schedule.telegram?.time ?? "",
      instagramDate: schedule.instagram?.date ?? "",
      instagramTime: schedule.instagram?.time ?? "",
      facebookDate: schedule.facebook?.date ?? "",
      facebookTime: schedule.facebook?.time ?? "",
      facebookPageUrl: schedule.facebook_page_url ?? driveAutomationSettings.facebook_page_url,
      timezone: schedule.timezone ?? "Asia/Karachi",
    });
    setSelectedVideoId(schedule.video_id);
    setModalError("");
    setModalOpen(true);
  };

  const submitSchedule = async () => {
    const payload: Record<string, unknown> = {
      video_id: draft.videoId,
      platforms: draft.platforms,
      timezone: draft.timezone,
    };
    for (const platform of publishingPlatforms) {
      const schedule = scheduleTimeFromDraft(draft, platform);
      payload[platform] = draft.platforms.includes(platform)
        ? { date: schedule.date || null, time: schedule.time || null, status: "scheduled" }
        : null;
    }
    if (draft.platforms.includes("facebook")) {
      payload.facebook_page_url = draft.facebookPageUrl;
    }

    try {
      setSavingSchedule(true);
      setModalError("");
      const url = editingScheduleId ? `/api/schedules/${editingScheduleId}` : "/api/schedules";
      const method = editingScheduleId ? "PUT" : "POST";
      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(data?.detail ?? "Schedule request failed");
      }
      if (editingScheduleId && data) {
        setSchedules((current) => current.map((schedule) =>
          schedule.schedule_id === editingScheduleId ? (data as Schedule) : schedule
        ));
      }
      await loadData();
      setModalOpen(false);
      setDraft(defaultForm());
      setEditingScheduleId(null);
    } catch (err) {
      setModalError(err instanceof Error ? err.message : "Unable to save schedule");
    } finally {
      setSavingSchedule(false);
    }
  };

  const retrySchedule = async (scheduleId: string) => {
    const response = await fetch(`/api/schedules/${scheduleId}/retry`, { method: "POST" });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.detail ?? "Retry failed");
    }
    await loadData();
  };

  const deleteSchedule = async (scheduleId: string) => {
    const response = await fetch(`/api/schedules/${scheduleId}`, { method: "DELETE" });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.detail ?? "Delete failed");
    }
    await loadData();
  };

  const runAction = async (action: () => Promise<void>) => {
    try {
      setError("");
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The requested action failed");
    }
  };

  const saveAutomationSettings = async () => {
    setSavingAutomation(true);
    setAutomationError("");
    setAutomationNotice("");
    try {
      const response = await fetch("/api/automation/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(automationSettings),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.detail ?? "Unable to save automation settings");
      setAutomationSettings(data as AutomationSettings);
      setAutomationNotice("Automation settings saved.");
    } catch (err) {
      setAutomationError(err instanceof Error ? err.message : "Unable to save automation settings");
    } finally {
      setSavingAutomation(false);
    }
  };

  const saveDriveAutomationSettings = async () => {
    setSavingDriveAutomation(true);
    setDriveAutomationError("");
    setDriveAutomationNotice("");
    try {
      const response = await fetch("/api/drive-automation/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(driveAutomationSettings),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(apiErrorMessage(data?.detail, "Unable to save Drive auto-publishing settings"));
      }
      setDriveAutomationSettings(data.settings as DriveAutomationSettings);
      setSavedDriveAutomationSettings(data.settings as DriveAutomationSettings);
      setEditingDriveAutomationSettings(false);
      const scheduledCount = Number(data.scheduled_count ?? 0);
      setDriveAutomationNotice(
        data.scheduling_error
          ? `Settings saved, but Drive videos could not be queued: ${data.scheduling_error}`
          : driveAutomationSettings.enabled
            ? `Settings saved. ${scheduledCount} available video(s) queued; new Drive videos will be queued automatically.`
            : "Drive auto-publishing settings saved.",
      );
    } catch (err) {
      setDriveAutomationError(err instanceof Error ? err.message : "Unable to save Drive auto-publishing settings");
    } finally {
      setSavingDriveAutomation(false);
    }
  };

  const editDriveAutomationSettings = () => {
    setDriveAutomationSettings(savedDriveAutomationSettings);
    setDriveAutomationError("");
    setDriveAutomationNotice("");
    setEditingDriveAutomationSettings(true);
  };

  const cancelDriveAutomationSettings = () => {
    setDriveAutomationSettings(savedDriveAutomationSettings);
    setDriveAutomationError("");
    setDriveAutomationNotice("");
    setEditingDriveAutomationSettings(false);
  };

  const openSocialLogin = async (platform: "instagram" | "facebook") => {
    setSocialBusy(platform);
    setSocialError("");
    setSocialNotice("");
    try {
      const response = await fetch(`/api/social-sessions/${platform}/login`, { method: "POST" });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.detail ?? `Unable to open ${platform} login`);
      setSocialSessions((current) => ({ ...current, [platform]: data as SocialSession }));
      setSocialNotice(`${capitalize(platform)} opened in Chrome. Sign in yourself and complete any security checks there.`);
    } catch (err) {
      setSocialError(err instanceof Error ? err.message : `Unable to open ${platform} login`);
    } finally {
      setSocialBusy(null);
    }
  };

  const completeSocialLogin = async (platform: "instagram" | "facebook") => {
    setSocialBusy(platform);
    setSocialError("");
    setSocialNotice("");
    try {
      const response = await fetch(`/api/social-sessions/${platform}/complete`, { method: "POST" });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.detail ?? `Unable to confirm ${platform} login`);
      setSocialSessions((current) => ({ ...current, [platform]: data as SocialSession }));
      setSocialNotice(`${capitalize(platform)} login saved in its local Chrome profile.`);
    } catch (err) {
      setSocialError(err instanceof Error ? err.message : `Unable to confirm ${platform} login`);
    } finally {
      setSocialBusy(null);
    }
  };

  const enqueueAutomationPrompts = async () => {
    const prompts = automationPrompts.split(/\r?\n/).map((prompt) => prompt.trim()).filter(Boolean);
    if (!prompts.length) {
      setAutomationError("Enter at least one prompt, with one topic on each line.");
      return;
    }
    setSavingAutomation(true);
    setAutomationError("");
    setAutomationNotice("");
    try {
      const response = await fetch("/api/automation/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompts }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.detail ?? "Unable to add prompts");
      setAutomationPrompts("");
      setAutomationNotice(`${(data as AutomationJob[]).length} prompt(s) added to the automation queue.`);
      await loadAutomation();
    } catch (err) {
      setAutomationError(err instanceof Error ? err.message : "Unable to add prompts");
    } finally {
      setSavingAutomation(false);
    }
  };

  const retryAutomationJob = async (jobId: string) => {
    const response = await fetch(`/api/automation/jobs/${jobId}/retry`, { method: "POST" });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.detail ?? "Unable to retry automation job");
    await loadAutomation();
  };

  const selectedVideo = videos.find((video) => video.id === (selectedVideoId || draft.videoId)) ?? videos[0];
  const visibleSchedules = schedules.filter((schedule) => {
    if (["YouTube", "Telegram", "Instagram", "Facebook"].includes(activeSection)) {
      return schedule.platforms.includes(activeSection.toLowerCase());
    }
    return true;
  });
  const showLibrary = activeSection === "Dashboard" || activeSection === "Videos";
  const showQueue = ["Dashboard", "Schedule", "YouTube", "Telegram", "Instagram", "Facebook"].includes(activeSection);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark channel-mark">
            <ThumbnailImage src={channel?.thumbnail} alt="" fallback="AI" />
          </div>
          <div>
            <strong>{channel?.title ?? "FlowCast"}</strong>
            <small>YouTube channel</small>
          </div>
        </div>
        <nav className="nav">
          {[
            "Dashboard",
            "Videos",
            "Schedule",
            "YouTube",
            "Telegram",
            "Instagram",
            "Facebook",
            "Automation",
            "Settings",
          ].map((item) => (
            <button
              className={`nav-item ${item === activeSection ? "active" : ""}`}
              key={item}
              onClick={() => {
                setActiveSection(item);
                setError("");
              }}
            >
              {item}
            </button>
          ))}
        </nav>
        <div className="sidebar-panel">
          <strong>System status</strong>
          <p>{workerHealth.running ? "Publishing worker is active." : "Publishing worker is not running. Restart the API server."}</p>
          {workerHealth.last_error ? <small className="worker-error">{workerHealth.last_error}</small> : null}
        </div>
      </aside>

      <div className="page-shell">
        <header className="topbar">
          <div>
            <span className="eyebrow">Video automation</span>
            <h1>{activeSection}</h1>
          </div>
          {activeSection !== "Automation" ? (
            <div className="top-actions">
              <button className="primary-btn" onClick={openCreateModal}>+ New schedule</button>
            </div>
          ) : null}
        </header>

        <main className="content">
          {activeSection === "Dashboard" ? (
            <>
              <p className="refresh-note">
                Drive library refreshes every 30 seconds. {driveAutomationSettings.enabled
                  ? `Available Drive videos are automatically scheduled for ${driveAutomationSettings.platforms.map(capitalize).join(", ")} every ${driveAutomationSettings.interval_minutes} minutes, up to ${driveAutomationSettings.max_videos_per_day} video(s) per platform per day.`
                  : "Videos are only scheduled when you create a schedule. Enable automatic publishing in Automation to queue future Drive uploads."}
              </p>
              <section className="stats-grid">
                {stats.map((item) => (
                  <article key={item.label} className="stat-card">
                    <div className={`stat-icon ${item.tone}`} aria-hidden="true">
                      {item.icon === "clock" ? (
                        <svg viewBox="0 0 24 24" className="stat-logo clock-logo">
                          <circle cx="12" cy="12" r="8.5" />
                          <path d="M12 7v5l3.25 2" />
                        </svg>
                      ) : null}
                      {item.icon === "youtube" ? (
                        <svg viewBox="0 0 24 24" className="stat-logo youtube-logo">
                          <rect x="2.5" y="5" width="19" height="14" rx="4" />
                          <path d="m10 8.5 5.5 3.5-5.5 3.5z" className="logo-cutout" />
                        </svg>
                      ) : null}
                      {item.icon === "telegram" ? (
                        <svg viewBox="0 0 24 24" className="stat-logo telegram-logo">
                          <path d="m21.5 3.2-3.3 17.1c-.2 1.2-.9 1.5-1.8.9l-5-3.7-2.4 2.3c-.3.3-.5.5-1 .5l.4-5.1 9.3-8.4c.4-.4-.1-.6-.6-.2L5.6 14l-4.9-1.5c-1.1-.3-1.1-1 .2-1.5L20 3c.9-.3 1.7.2 1.5.2Z" />
                        </svg>
                      ) : null}
                      {item.icon === "instagram" || item.icon === "facebook" ? (
                        <SocialPlatformIcon
                          platform={item.icon}
                          className={`stat-logo ${item.icon}-logo`}
                        />
                      ) : null}
                      {item.icon === "drive" ? (
                        <svg viewBox="0 0 24 24" className="stat-logo drive-logo">
                          <path d="M8.1 3h6.1l7.3 12.6h-6.1z" className="drive-green" />
                          <path d="M8.1 3 1 15.6l3.1 5.3 7.3-12.6z" className="drive-blue" />
                          <path d="M4.1 20.9h14.7l3.1-5.3H7.2z" className="drive-yellow" />
                        </svg>
                      ) : null}
                    </div>
                    <div>
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                    </div>
                  </article>
                ))}
              </section>
            </>
          ) : null}

          {error ? <div className="notice error">{error}</div> : null}

          {activeSection === "Settings" ? (
            <section className="panel settings-panel">
              <span className="section-label">Connections</span>
              <h2>Connected services</h2>
              <p>Google Drive and YouTube use the existing Google OAuth token. Groq metadata is generated from the server-side environment configuration.</p>
              <p><strong>YouTube channel:</strong> {channel?.title ?? "Unavailable — check Google authentication"}</p>
              <p><strong>Publishing worker:</strong> {workerHealth.running ? "Active" : "Not running"}. Due jobs are checked every 15 seconds.</p>
              {workerHealth.last_checked_at ? <p><strong>Last scheduler check:</strong> {new Date(workerHealth.last_checked_at).toLocaleString()}</p> : null}
              {workerHealth.last_error ? <p className="worker-error"><strong>Last scheduler error:</strong> {workerHealth.last_error}</p> : null}
            </section>
          ) : null}

          {activeSection === "Automation" ? (
            <div className="automation-layout">
              <section className="panel automation-settings drive-auto-panel">
                <div className="panel-header">
                  <span className="section-label">New Drive uploads</span>
                  <h2>Automatic Drive publishing</h2>
                  <p>Choose YouTube, Instagram, and/or Facebook, then set the spacing in minutes and daily limit. Saving immediately queues available Drive videos in order up to each platform's daily limit; later videos are queued as Drive receives them. Existing schedules and already-published videos are skipped. You can still edit pending schedule times individually.</p>
                </div>
                <p className="automation-worker-status">
                  Drive watcher: {driveAutomationHealth.running ? "active" : "not running — start the uploader dashboard API"}.
                  {driveAutomationHealth.last_error ? ` ${driveAutomationHealth.last_error}` : ""}
                </p>
                {driveAutomationError ? <div className="notice error">{driveAutomationError}</div> : null}
                {driveAutomationNotice ? <div className="notice success">{driveAutomationNotice}</div> : null}
                {!editingDriveAutomationSettings ? (
                  <div className="automation-settings-summary">
                    <p>
                      Auto-publishing is <strong>{savedDriveAutomationSettings.enabled ? "enabled" : "disabled"}</strong> for{" "}
                      <strong>{savedDriveAutomationSettings.platforms.map(capitalize).join(", ")}</strong>.
                    </p>
                    <p>
                      Every {savedDriveAutomationSettings.interval_minutes} minutes · up to{" "}
                      {savedDriveAutomationSettings.max_videos_per_day} video(s) per platform per day ·{" "}
                      {savedDriveAutomationSettings.timezone}
                    </p>
                    {savedDriveAutomationSettings.platforms.includes("facebook") ? (
                      <p>Facebook Page: {savedDriveAutomationSettings.facebook_page_url}</p>
                    ) : null}
                    <button
                      type="button"
                      className="secondary-btn"
                      onClick={editDriveAutomationSettings}
                    >
                      Change settings
                    </button>
                  </div>
                ) : (
                  <>
                <label className="automation-toggle">
                  <input
                    type="checkbox"
                    checked={driveAutomationSettings.enabled}
                    onChange={(event) => setDriveAutomationSettings((current) => ({ ...current, enabled: event.target.checked }))}
                  />
                  <span>
                    Automatically schedule new Drive videos for {driveAutomationSettings.platforms.map(capitalize).join(", ")}
                  </span>
                </label>
                <div className="automation-fields">
                  <label>
                    <span>Minutes between scheduled videos</span>
                    <input
                      type="number"
                      min={1}
                      max={1440}
                      step={1}
                      required
                      value={driveAutomationSettings.interval_minutes}
                      onChange={(event) => setDriveAutomationSettings((current) => ({
                        ...current,
                        interval_minutes: Number(event.target.value),
                      }))}
                    />
                    <small>Choose 1 to 1,440 minutes (up to 24 hours).</small>
                  </label>
                  <label>
                    <span>Maximum videos per platform per day</span>
                    <input
                      type="number"
                      min={1}
                      max={50}
                      step={1}
                      value={driveAutomationSettings.max_videos_per_day}
                      onChange={(event) => setDriveAutomationSettings((current) => ({
                        ...current,
                        max_videos_per_day: Number(event.target.value),
                      }))}
                    />
                    <small>Includes other queued posts on that platform and calendar day.</small>
                  </label>
                  <label>
                    <span>Timezone</span>
                    <input
                      value={driveAutomationSettings.timezone}
                      onChange={(event) => setDriveAutomationSettings((current) => ({ ...current, timezone: event.target.value }))}
                      placeholder="Asia/Karachi"
                    />
                  </label>
                  {driveAutomationSettings.platforms.includes("facebook") ? (
                    <label>
                      <span>Facebook Page URL</span>
                      <input
                        value={driveAutomationSettings.facebook_page_url}
                        onChange={(event) => setDriveAutomationSettings((current) => ({
                          ...current,
                          facebook_page_url: event.target.value,
                        }))}
                        placeholder="https://www.facebook.com/your-page"
                      />
                      <small>Paste the HTTPS Page URL from facebook.com, web.facebook.com, or m.facebook.com.</small>
                    </label>
                  ) : null}
                </div>
                <div className="platforms-selection">
                  {drivePublishingPlatforms.map((platform) => (
                    <button
                      key={platform}
                      type="button"
                      className={`platform-card ${driveAutomationSettings.platforms.includes(platform) ? "selected" : ""}`}
                      onClick={() => setDriveAutomationSettings((current) => ({
                        ...current,
                        platforms: current.platforms.includes(platform)
                          ? current.platforms.length === 1
                            ? current.platforms
                            : current.platforms.filter((item) => item !== platform)
                          : [...current.platforms, platform],
                      }))}
                    >
                      {platform === "instagram" || platform === "facebook" ? (
                        <SocialPlatformIcon platform={platform} className="platform-logo" />
                      ) : null}
                      {capitalize(platform)}
                    </button>
                  ))}
                </div>
                <button type="button" className="primary-btn" disabled={savingDriveAutomation} onClick={() => void saveDriveAutomationSettings()}>
                  {savingDriveAutomation ? "Saving..." : "Save Drive auto-publishing"}
                </button>
                <button
                  type="button"
                  className="secondary-btn"
                  disabled={savingDriveAutomation}
                  onClick={cancelDriveAutomationSettings}
                >
                  Cancel
                </button>
                  </>
                )}
                <p className="automation-worker-status">Instagram and Facebook each reuse one local Chrome browser and profile across scheduled videos. Sign in yourself in the opened browser; passwords and verification codes are never collected by the app.</p>
                {socialError ? <div className="notice error">{socialError}</div> : null}
                {socialNotice ? <div className="notice success">{socialNotice}</div> : null}
                <div className="social-login-list">
                  {(["instagram", "facebook"] as const).map((platform) => {
                    const session = socialSessions[platform];
                    return (
                      <div className="social-login-row" key={platform}>
                        <div>
                          <strong>{capitalize(platform)}</strong>
                          <small>
                            {session.authenticated
                              ? session.browser_open ? "Signed in · Chrome open" : "Signed in · profile saved"
                              : session.browser_open ? "Chrome open · finish signing in" : "Not signed in"}
                          </small>
                        </div>
                        <div className="action-buttons">
                          <button type="button" disabled={socialBusy !== null} onClick={() => void openSocialLogin(platform)}>
                            {socialBusy === platform ? "Opening..." : session.browser_open ? "Show login window" : "Open login"}
                          </button>
                          {session.browser_open ? (
                            <button type="button" disabled={socialBusy !== null} onClick={() => void completeSocialLogin(platform)}>
                              {socialBusy === platform ? "Checking..." : "Save login"}
                            </button>
                          ) : null}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <p className="automation-worker-status">Keep the API running on the same computer as Chrome. After each scheduled upload, the browser stays open while the API is running; closing/restarting the API closes Chrome, but the saved profile remains for the next run.</p>
              </section>

              <section className="panel automation-settings">
                <div className="panel-header">
                  <span className="section-label">AI video pipeline</span>
                  <h2>Prompt queue and publishing defaults</h2>
                  <p>Two different AI providers prepare and review each brief. Configure them in video-generator-2.0/backend/.env (defaults: Gemini and GLM). The video generator renders a named MP4 to Drive, then reserves the next open publishing slot.</p>
                </div>
                <p className="automation-worker-status">
                  Automation worker: {automationHealth.running ? "active" : "not running — start the uploader dashboard API"}.
                  {automationHealth.last_error ? ` ${automationHealth.last_error}` : ""}
                </p>
                {automationError ? <div className="notice error">{automationError}</div> : null}
                {automationNotice ? <div className="notice success">{automationNotice}</div> : null}
                <label className="automation-toggle">
                  <input
                    type="checkbox"
                    checked={automationSettings.enabled}
                    onChange={(event) => setAutomationSettings((current) => ({ ...current, enabled: event.target.checked }))}
                  />
                  <span>Run automation for queued prompts</span>
                </label>
                <div className="automation-fields">
                  <label>
                    <span>Timezone</span>
                    <input
                      value={automationSettings.timezone}
                      onChange={(event) => setAutomationSettings((current) => ({ ...current, timezone: event.target.value }))}
                      placeholder="Asia/Karachi"
                    />
                  </label>
                  <label>
                    <span>YouTube publish times (24-hour, comma-separated)</span>
                    <input
                      value={automationSettings.youtube_times.join(", ")}
                      onChange={(event) => setAutomationSettings((current) => ({
                        ...current,
                        youtube_times: event.target.value.split(",").map((value) => value.trim()).filter(Boolean),
                      }))}
                      placeholder="17:30, 21:30"
                    />
                  </label>
                  <label>
                    <span>Telegram publish times (24-hour, comma-separated)</span>
                    <input
                      value={automationSettings.telegram_times.join(", ")}
                      onChange={(event) => setAutomationSettings((current) => ({
                        ...current,
                        telegram_times: event.target.value.split(",").map((value) => value.trim()).filter(Boolean),
                      }))}
                      placeholder="17:30, 21:30"
                    />
                  </label>
                </div>
                <div className="platforms-selection">
                  {(["youtube", "telegram"] as PromptPlatform[]).map((platform) => (
                    <button
                      key={platform}
                      type="button"
                      className={`platform-card ${automationSettings.platforms.includes(platform) ? "selected" : ""}`}
                      onClick={() => setAutomationSettings((current) => ({
                        ...current,
                        platforms: current.platforms.includes(platform)
                          ? current.platforms.filter((item) => item !== platform)
                          : [...current.platforms, platform],
                      }))}
                    >
                      {platform === "instagram" || platform === "facebook" ? (
                        <SocialPlatformIcon platform={platform} className="platform-logo" />
                      ) : null}
                      {capitalize(platform)}
                    </button>
                  ))}
                </div>
                <button type="button" className="primary-btn" disabled={savingAutomation} onClick={() => void saveAutomationSettings()}>
                  {savingAutomation ? "Saving..." : "Save automation settings"}
                </button>
              </section>

              <section className="panel automation-prompt-panel">
                <div className="panel-header">
                  <span className="section-label">Prepared topics</span>
                  <h2>Add prompts</h2>
                  <p>Enter one video topic or prompt per line. Jobs run in order and continue automatically while the dashboard API and generator worker are running.</p>
                </div>
                <label>
                  <span>Video topics</span>
                  <textarea
                    rows={5}
                    value={automationPrompts}
                    onChange={(event) => setAutomationPrompts(event.target.value)}
                    placeholder={"Practical travel tips for first-time visitors to Japan\nHow to avoid hidden fees when traveling abroad"}
                  />
                </label>
                <button type="button" className="primary-btn" disabled={savingAutomation || !automationPrompts.trim()} onClick={() => void enqueueAutomationPrompts()}>
                  {savingAutomation ? "Working..." : "Add prompts to queue"}
                </button>
              </section>

              <section className="panel automation-jobs">
                <div className="panel-header">
                  <span className="section-label">Automation status</span>
                  <h2>Jobs</h2>
                </div>
                {automationJobs.length === 0 ? <div className="empty-state">No automation prompts yet.</div> : (
                  <div className="automation-job-list">
                    {automationJobs.map((job) => (
                      <article className="automation-job" key={job.job_id}>
                        <div className="automation-job-heading">
                          <Badge text={job.status} tone={job.status === "failed" ? "red" : job.status === "scheduled" ? "green" : "purple"} />
                          <small>{new Date(job.created_at).toLocaleString()}</small>
                        </div>
                        <strong>{job.prompt}</strong>
                        <p>{job.stage}{job.progress_percent > 0 && job.status !== "scheduled" ? ` · ${job.progress_percent}%` : ""}</p>
                        {job.drive_filename ? <small>Drive file: {job.drive_filename}</small> : null}
                        {job.idea_provider || job.review_provider ?                         <small>Models: {job.idea_provider ?? "idea"} -&gt; {job.review_provider ?? "review"}</small> : null}
                        {job.drive_url ? <a href={job.drive_url} target="_blank" rel="noreferrer">Open saved video in Drive</a> : null}
                        {job.last_error ? <p className="row-error">{job.last_error}</p> : null}
                        {job.status === "failed" ? (
                          <button type="button" className="secondary-btn" onClick={() => void runAction(() => retryAutomationJob(job.job_id))}>Retry</button>
                        ) : null}
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </div>
          ) : null}

          {showLibrary ? <section className="panel-grid">
            <div className="panel large-panel">
              <div className="panel-header">
                <div>
                  <span className="section-label">Drive library</span>
                  <h2>Available videos</h2>
                </div>
              </div>
              <div className="video-list">
                {videos.length === 0 && !loading ? <div className="empty-state">No unprocessed, unscheduled videos are currently available in the configured Google Drive folder.</div> : null}
                {videos.map((video) => (
                  <button
                    key={video.id}
                    type="button"
                    className={`video-row ${selectedVideo?.id === video.id ? "selected" : ""}`}
                    onClick={() => {
                      setSelectedVideoId(video.id);
                      setDraft((current) => ({ ...current, videoId: video.id }));
                    }}
                  >
                    <div className="row-thumb">
                      <ThumbnailImage src={video.thumbnail} alt={video.filename} fallback="Video" />
                    </div>
                    <div className="row-copy">
                      <strong>{video.filename}</strong>
                      <small>
                        {video.size ? `${Math.round(video.size / 1024 / 1024)} MB` : "Unknown size"} • {video.created_at ? formatDateLabel(video.created_at) : "Today"}
                      </small>
                    </div>
                    <Badge text={video.status} tone="green" />
                  </button>
                ))}
              </div>
            </div>

            <div className="panel small-panel">
              <div className="panel-header">
                <div>
                  <span className="section-label">Selected video</span>
                  <h2>Queue preview</h2>
                </div>
              </div>
              {selectedVideo ? (
                <div className="preview-card">
                  <div className="preview-thumb">
                    <ThumbnailImage src={selectedVideo.thumbnail} alt={selectedVideo.filename} fallback="Preview" />
                  </div>
                  <strong>{selectedVideo.filename}</strong>
                  <p>{selectedVideo.duration ? `Duration: ${formatDuration(Number(selectedVideo.duration))}` : "Metadata ready for scheduling."}</p>
                  <button type="button" className="primary-btn" onClick={openCreateModal}>Schedule this video</button>
                </div>
              ) : (
                <div className="empty-state small">Choose a video from the drive library.</div>
              )}
            </div>
          </section> : null}

          {showQueue ? <section className="panel schedules-panel">
            <div className="panel-header">
              <div>
                <span className="section-label">Publishing queue</span>
                <h2>{activeSection === "YouTube" || activeSection === "Telegram" ? `${activeSection} schedules` : "Scheduled videos"}</h2>
              </div>
            </div>
            <div className="schedule-table">
              <div className="table-head">
                <span>Video</span>
                <span>Platforms</span>
                <span>Schedule</span>
                <span>Status</span>
                <span>Actions</span>
              </div>

              {visibleSchedules.length === 0 ? (
                <div className="empty-state table-empty">There are no active schedules in this view.</div>
              ) : (
                visibleSchedules.map((schedule) => (
                  <div className="table-row" key={schedule.schedule_id}>
                    <div className="video-meta">
                      <div className="mini-thumb">
                        <ThumbnailImage src={schedule.thumbnail} alt={schedule.filename} fallback="V" />
                      </div>
                      <div>
                        <strong>{schedule.filename}</strong>
                        <small>{schedule.timezone}</small>
                      </div>
                    </div>
                    <div className="platforms">
                      {schedule.platforms.map((platformName) => {
                        const platform = platformName as Platform;
                        const delivery = getPlatformSchedule(schedule, platform);
                        const platformStatus = delivery?.status ?? "scheduled";
                        const statusTone = platformStatus === "failed"
                          ? "red"
                          : ["published", "sent"].includes(platformStatus)
                            ? "green"
                            : platformStatus === "processing"
                              ? "purple"
                              : "neutral";
                        return (
                          <div className="platform-status-item" key={`${schedule.schedule_id}-${platform}`}>
                            <Badge text={capitalize(platform)} tone={platform === "youtube" ? "red" : "blue"} />
                            <Badge text={capitalize(platformStatus)} tone={statusTone} />
                          </div>
                        );
                      })}
                    </div>
                    <div className="schedule-dates">
                      {schedule.platforms.map((platformName) => {
                        const platform = platformName as Platform;
                        const target = getPlatformSchedule(schedule, platform);
                        return (
                          <span key={`${schedule.schedule_id}-${platform}-time`}>
                            <strong>{capitalize(platform)}</strong>
                            {formatDateLabel(target.date)} • {formatTimeLabel(target.time)}
                          </span>
                        );
                      })}
                    </div>
                    <Badge text={schedule.status || "scheduled"} tone={schedule.status === "failed" ? "red" : "green"} />
                    <div className="action-buttons">
                      {schedule.status === "scheduled" || schedule.status === "failed" ? (
                        <>
                          <button type="button" onClick={() => openEditModal(schedule)}>Edit</button>
                          {schedule.status === "failed" ? (
                            <button
                              type="button"
                              className="danger"
                              onClick={() => {
                                const delivered = hasDeliveredPlatform(schedule);
                                const confirmation = delivered
                                  ? `Remove the failed schedule record for ${schedule.filename}? Any platform that already published the video will remain published.`
                                  : `Delete the failed schedule for ${schedule.filename}?`;
                                if (window.confirm(confirmation)) {
                                  void runAction(() => deleteSchedule(schedule.schedule_id));
                                }
                              }}
                            >Delete failed</button>
                          ) : (
                            <button
                              type="button"
                              className="danger"
                              onClick={() => {
                                if (window.confirm(`Cancel the schedule for ${schedule.filename}?`)) {
                                  void runAction(() => deleteSchedule(schedule.schedule_id));
                                }
                              }}
                            >Cancel</button>
                          )}
                        </>
                      ) : null}
                      {schedule.status === "failed" ? (
                        <button type="button" onClick={() => void runAction(() => retrySchedule(schedule.schedule_id))}>Retry failed</button>
                      ) : null}
                    </div>
                    {schedule.last_error ? <p className="row-error">{schedule.last_error}</p> : null}
                  </div>
                ))
              )}
            </div>
          </section> : null}
        </main>
      </div>

      {modalOpen ? (
        <div className="modal-backdrop" onClick={() => !savingSchedule && setModalOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <div>
                <span className="section-label">{editingScheduleId ? "Update schedule" : "Create schedule"}</span>
                <h2>{editingScheduleId ? "Reschedule job" : "Schedule video"}</h2>
              </div>
              <button type="button" className="icon-close" disabled={savingSchedule} onClick={() => setModalOpen(false)}>×</button>
            </div>

            <div className="modal-scroll">
            <div className="form-grid">
              <label>
                <span>Video</span>
                <select
                  value={draft.videoId}
                  onChange={(event) => setDraft((current) => ({ ...current, videoId: event.target.value }))}
                >
                  {videos.map((video) => (
                    <option key={video.id} value={video.id}>{video.filename}</option>
                  ))}
                  {editingScheduleId && !videos.some((video) => video.id === draft.videoId) ? (
                    <option value={draft.videoId}>{schedules.find((schedule) => schedule.schedule_id === editingScheduleId)?.filename ?? draft.videoId}</option>
                  ) : null}
                </select>
              </label>

              <label>
                <span>Timezone</span>
                <input
                  value={draft.timezone}
                  onChange={(event) => setDraft((current) => ({ ...current, timezone: event.target.value }))}
                />
              </label>
            </div>

            {modalError ? <div className="notice error modal-error">{modalError}</div> : null}

            <div className="platforms-selection">
              {publishingPlatforms.map((platform) => (
                <button
                  key={platform}
                  type="button"
                  className={`platform-card ${draft.platforms.includes(platform) ? "selected" : ""}`}
                  onClick={() => {
                    setDraft((current) => {
                      const selected = current.platforms.includes(platform)
                      if (selected && current.platforms.length === 1) return current;
                      return {
                        ...current,
                        platforms: selected ? current.platforms.filter((item) => item !== platform) : [...current.platforms, platform],
                      };
                    });
                  }}
                >
                  {capitalize(platform)}
                </button>
              ))}
            </div>

            {draft.platforms.includes("facebook") ? (
              <label>
                <span>Facebook Page URL</span>
                <input
                  value={draft.facebookPageUrl}
                  onChange={(event) => setDraft((current) => ({
                    ...current,
                    facebookPageUrl: event.target.value,
                  }))}
                  placeholder="https://www.facebook.com/your-page"
                />
              </label>
            ) : null}

            <div className="schedule-fields">
              {draft.platforms.map((platform) => {
                const target = scheduleTimeFromDraft(draft, platform);
                return (
                  <div className="subcard" key={platform}>
                    <h3>{capitalize(platform)}</h3>
                    <div className="field-grid">
                      <label>
                        <span>Date</span>
                        <input
                          type="date"
                          value={target.date}
                          onChange={(event) => setDraft((current) => ({
                            ...current,
                            [`${platform}Date`]: event.target.value,
                          }))}
                        />
                      </label>
                      <label>
                        <span>Time</span>
                        <input
                          type="time"
                          value={target.time}
                          onChange={(event) => setDraft((current) => ({
                            ...current,
                            [`${platform}Time`]: event.target.value,
                          }))}
                        />
                      </label>
                    </div>
                  </div>
                );
              })}
            </div>
            </div>

            <div className="modal-actions">
              <button type="button" className="secondary-btn" disabled={savingSchedule} onClick={() => setModalOpen(false)}>Cancel</button>
              <button
                type="button"
                className="primary-btn"
                disabled={savingSchedule || !draft.videoId || draft.platforms.length === 0 || draft.platforms.some((platform) => {
                  const target = scheduleTimeFromDraft(draft, platform);
                  return !target.date || !target.time;
                })}
                onClick={() => void submitSchedule()}
              >{savingSchedule ? "Saving..." : editingScheduleId ? "Save changes" : "Create schedule"}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default App;
