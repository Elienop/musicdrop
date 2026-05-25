import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

async function fetchHealth() {
  const { data, error } = await client.GET("/api/health");
  if (error || !data) {
    throw new Error("Health check failed");
  }
  return data;
}

export function HealthIndicator() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backend</CardTitle>
        <CardDescription>FastAPI health check via /api/health</CardDescription>
      </CardHeader>
      <CardContent>
        {isPending ? (
          <Skeleton className="h-6 w-24" />
        ) : isError ? (
          <Badge variant="destructive">unreachable</Badge>
        ) : (
          <div className="flex items-center gap-2">
            <Badge>{data.status}</Badge>
            <span className="text-muted-foreground text-sm">
              v{data.version}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
