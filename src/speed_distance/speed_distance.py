import numpy as np
import cv2
from collections import defaultdict, deque
from utils import measure_distance, get_foot_position

class ViewTransformer:
    def __init__(self):
        # Set physical court dimensions in meters
        court_width = 68
        court_length = 105  # Full football pitch length
        
        # Coordinates of the four corners in the video frame (manually measured)
        self.pixel_vertices = np.array([[110, 1035],
                                        [265, 275],
                                        [910, 260],
                                        [1640, 915]], dtype=np.float32)
        
        # Corresponding real-world points (in meters)
        self.target_vertices = np.array([[0, court_width],
                                         [0, 0],
                                         [court_length, 0],
                                         [court_length, court_width]], dtype=np.float32)
        
        self.perspective_transformer = cv2.getPerspectiveTransform(self.pixel_vertices, self.target_vertices)
    
    def transform_point(self, point):
        """Transform a point from pixel coordinates to real-world coordinates"""
        try:
            p = (int(point[0]), int(point[1]))
            
            # Check if point is within the transformation region
            if cv2.pointPolygonTest(self.pixel_vertices, p, False) < 0:
                return None
            
            reshaped_point = np.array(point, dtype=np.float32).reshape(-1, 1, 2)
            transformed_point = cv2.perspectiveTransform(reshaped_point, self.perspective_transformer)
            return transformed_point.reshape(-1, 2)
        except Exception as e:
            print(f"Transform error for point {point}: {e}")
            return None
    
    def add_transformed_position_to_tracks(self, tracks):
        """Add transformed positions to all tracks"""
        for obj, obj_tracks in tracks.items():
            for frame_idx, track in enumerate(obj_tracks):
                for tid, info in track.items():
                    pos = info.get('position_adjusted')
                    if pos is None:
                        continue
                    
                    pos = np.array(pos, dtype=np.float32)
                    transformed = self.transform_point(pos)
                    
                    if transformed is not None:
                        tracks[obj][frame_idx][tid]['position_transformed'] = transformed.squeeze().tolist()


class SpeedAndDistanceEstimator:
    def __init__(self, frame_rate=30, smoothing_window=5, max_reasonable_speed=40.0, frame_window=None):
        self.frame_rate = frame_rate
        # Use frame_window if provided for backward compatibility, otherwise use smoothing_window
        self.smoothing_window = frame_window if frame_window is not None else smoothing_window
        self.max_reasonable_speed = max_reasonable_speed  # km/h - maximum reasonable player speed
        
        # Track history for each player
        self.position_history = defaultdict(lambda: deque(maxlen=20))  # Store last 20 positions with coord type
        self.speed_history = defaultdict(lambda: deque(maxlen=10))     # Store last 10 speed calculations
        self.distance_traveled = defaultdict(float)                    # Total distance per player
        
        # Cache for display values to avoid flickering
        self.display_cache = {}
        
        # Pixel to meter conversion factor (rough approximation)
        self.pixel_to_meter_ratio = 10.0  # Adjust based on your video resolution and field view
        
    def _calculate_instantaneous_speed(self, positions, frame_indices, use_pixel_coords=False):
        """Calculate speed using multiple position points for better accuracy"""
        if len(positions) < 2:
            return None
            
        # Use the most recent positions for calculation
        recent_positions = positions[-min(self.smoothing_window, len(positions)):]
        recent_indices = frame_indices[-min(self.smoothing_window, len(frame_indices)):]
        
        if len(recent_positions) < 2:
            return None
            
        # Calculate distance over the time window
        total_distance = 0
        for i in range(1, len(recent_positions)):
            if use_pixel_coords:
                # For pixel coordinates, use a simple conversion factor
                # Assuming roughly 10 pixels per meter (this is approximate)
                pixel_distance = np.sqrt((recent_positions[i][0] - recent_positions[i-1][0])**2 + 
                                       (recent_positions[i][1] - recent_positions[i-1][1])**2)
                dist = pixel_distance / 10.0  # Convert pixels to meters (rough approximation)
            else:
                # Use real-world coordinates
                dist = measure_distance(recent_positions[i-1], recent_positions[i])
            total_distance += dist
            
        # Calculate time difference
        time_diff = (recent_indices[-1] - recent_indices[0]) / self.frame_rate
        
        if time_diff <= 0:
            return None
            
        # Speed in m/s, then convert to km/h
        speed_mps = total_distance / time_diff
        speed_kmph = speed_mps * 3.6
        
        # Apply reasonable speed limits (professional players rarely exceed 35-40 km/h)
        if speed_kmph > self.max_reasonable_speed:
            return None
            
        return speed_kmph
    
    def _calculate_distance_increment(self, prev_pos, curr_pos, use_pixel_coords=False):
        """Calculate distance increment between two positions"""
        try:
            if use_pixel_coords:
                # For pixel coordinates, use conversion factor
                pixel_distance = np.sqrt((curr_pos[0] - prev_pos[0])**2 + 
                                       (curr_pos[1] - prev_pos[1])**2)
                distance = pixel_distance / 10.0  # Convert pixels to meters
                
                # Reasonable limit for pixel coordinates (less movement per frame)
                if distance > 2.0:  # Less than 2 meters per frame
                    return None
            else:
                # Use real-world coordinates
                distance = measure_distance(prev_pos, curr_pos)
                
                # Only add distance if movement seems reasonable
                if distance > 5.0:  # Less than 5 meters per frame
                    return None
            
            return distance
        except Exception:
            return None
    
    def _smooth_speed(self, player_id, new_speed):
        """Apply smoothing to speed calculations"""
        if new_speed is None:
            return None
            
        speed_hist = self.speed_history[player_id]
        speed_hist.append(new_speed)
        
        # Return median of recent speeds for stability
        if len(speed_hist) >= 3:
            return np.median(list(speed_hist))
        else:
            return new_speed
    
    def add_speed_and_distance_to_tracks(self, tracks):
        """Add speed and distance calculations to tracks"""
        for obj, obj_tracks in tracks.items():
            if obj in ("ball", "referees"):
                continue
                
            # Process each player
            for frame_idx, track in enumerate(obj_tracks):
                for tid, info in track.items():
                    player_key = f"{obj}_{tid}"
                    
                    # Try to get transformed position first, fallback to pixel position
                    position = info.get("position_transformed")
                    use_pixel_coords = False
                    
                    if position is None:
                        # Use pixel coordinates as fallback
                        pixel_pos = info.get("position_adjusted") or info.get("position")
                        if pixel_pos is not None:
                            position = pixel_pos
                            use_pixel_coords = True
                    
                    if position is None:
                        # If we still have cached data, keep showing it
                        if player_key in self.display_cache:
                            info["speed"] = self.display_cache[player_key]['speed']
                            info["distance"] = self.display_cache[player_key]['distance']
                        continue
                    
                    # Store position in history
                    pos_hist = self.position_history[player_key]
                    pos_hist.append((position, frame_idx, use_pixel_coords))
                    
                    # Calculate speed if we have enough history
                    if len(pos_hist) >= 2:
                        # Get recent positions and check if they're consistent coordinate types
                        recent_positions = []
                        recent_indices = []
                        coord_type_consistent = True
                        
                        for pos_data in list(pos_hist)[-self.smoothing_window:]:
                            pos, idx, is_pixel = pos_data
                            recent_positions.append(pos)
                            recent_indices.append(idx)
                            
                            # Check if coordinate types are mixed
                            if is_pixel != use_pixel_coords:
                                coord_type_consistent = False
                        
                        if len(recent_positions) >= 2 and coord_type_consistent:
                            # Calculate speed
                            raw_speed = self._calculate_instantaneous_speed(recent_positions, recent_indices, use_pixel_coords)
                            smoothed_speed = self._smooth_speed(player_key, raw_speed)
                            
                            # Calculate incremental distance
                            if len(pos_hist) >= 2:
                                prev_pos_data = pos_hist[-2]
                                curr_pos_data = pos_hist[-1]
                                
                                # Only calculate if coordinate types match
                                if prev_pos_data[2] == curr_pos_data[2]:
                                    distance_increment = self._calculate_distance_increment(
                                        prev_pos_data[0], curr_pos_data[0], use_pixel_coords
                                    )
                                    
                                    if distance_increment is not None:
                                        self.distance_traveled[player_key] += distance_increment
                            
                            # Store results
                            if smoothed_speed is not None:
                                info["speed"] = smoothed_speed
                                info["distance"] = self.distance_traveled[player_key]
                                
                                # Update display cache
                                self.display_cache[player_key] = {
                                    'speed': smoothed_speed,
                                    'distance': self.distance_traveled[player_key]
                                }
                        else:
                            # Mixed coordinate types or insufficient data, use cached values
                            if player_key in self.display_cache:
                                info["speed"] = self.display_cache[player_key]['speed']
                                info["distance"] = self.display_cache[player_key]['distance']
                    else:
                        # Not enough history, but initialize with zero values
                        if player_key not in self.display_cache:
                            self.display_cache[player_key] = {'speed': 0.0, 'distance': 0.0}
                            info["speed"] = 0.0
                            info["distance"] = 0.0
    
    def draw_speed_and_distance(self, frames, tracks):
        """Draw speed and distance information on frames"""
        output = []
        
        for frame_idx, frame in enumerate(frames):
            img = frame.copy()
            
            for obj, obj_tracks in tracks.items():
                if obj in ("ball", "referees"):
                    continue
                
                if frame_idx >= len(obj_tracks):
                    continue
                    
                for tid, info in obj_tracks[frame_idx].items():
                    player_key = f"{obj}_{tid}"
                    
                    # Get speed and distance from track info or cache
                    speed = info.get("speed")
                    distance = info.get("distance")
                    
                    # Fall back to cache if current values are not available
                    if speed is None or distance is None:
                        cached = self.display_cache.get(player_key)
                        if cached:
                            speed = cached.get('speed', speed)
                            distance = cached.get('distance', distance)
                    
                    # Skip if we still don't have values
                    if speed is None or distance is None:
                        continue
                    
                    # Get bounding box and calculate foot position
                    bbox = info.get("bbox")
                    if bbox is None:
                        continue
                    
                    try:
                        foot_pos = get_foot_position(bbox)
                        text_x = int(foot_pos[0])
                        text_y = int(foot_pos[1] + 40)
                        
                        # Ensure text position is within frame bounds
                        h, w = img.shape[:2]
                        text_x = max(0, min(text_x, w - 100))
                        text_y = max(20, min(text_y, h - 40))
                        
                        # Draw speed and distance text without background
                        speed_text = f"{speed:.1f} km/h"
                        dist_text = f"{distance:.1f} m"
                        
                        # Draw text directly
                        cv2.putText(img, speed_text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 2)
                        cv2.putText(img, dist_text, (text_x, text_y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 2)
                        
                    except Exception as e:
                        print(f"Error drawing text for player {tid}: {e}")
                        continue
            
            output.append(img)
        
        return output
    
    def reset_player_data(self, player_key=None):
        """Reset tracking data for a specific player or all players"""
        if player_key:
            if player_key in self.position_history:
                del self.position_history[player_key]
            if player_key in self.speed_history:
                del self.speed_history[player_key]
            if player_key in self.distance_traveled:
                del self.distance_traveled[player_key]
            if player_key in self.display_cache:
                del self.display_cache[player_key]
        else:
            # Reset all
            self.position_history.clear()
            self.speed_history.clear()
            self.distance_traveled.clear()
            self.display_cache.clear()
    
    def get_player_stats(self):
        """Get statistics for all tracked players"""
        stats = {}
        for player_key in self.distance_traveled:
            speed_hist = list(self.speed_history.get(player_key, []))
            stats[player_key] = {
                'total_distance': self.distance_traveled[player_key],
                'avg_speed': np.mean(speed_hist) if speed_hist else 0,
                'max_speed': np.max(speed_hist) if speed_hist else 0,
                'current_speed': speed_hist[-1] if speed_hist else 0
            }
        return stats