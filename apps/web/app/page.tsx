import { redirect } from "next/navigation";

/** The dashboard has one destination for now. */
export default function Home() {
  redirect("/traces");
}
